from __future__ import annotations

import ctypes
import hashlib
import json
import ntpath
import os
import re
import secrets
import threading
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .windows_security import (
    WindowsSecurityError,
    current_user_sid,
    ensure_owner_private_directory,
    owner_only_security_attributes,
    validate_owner_private_path,
    validate_windows_path_no_reparse,
)
from .windows_tls import WindowsTLSError, WindowsTLSMaterial, load_windows_tls_material


_MANIFEST_NAME = ".factlane-firefox-trust.json"
_MANIFEST_VERSION = 1
_NSS_DLL_NAME = "nss3.dll"
_NSS_DB_FILES = ("cert9.db", "key4.db", "pkcs11.txt")
_FACTLANE_CA_NICKNAME = b"FactLane Private CA"
_MAX_MANIFEST_BYTES = 16 * 1024
_PROFILE_PREFS_NAME = "user.js"
_MAX_PROFILE_PREFS_BYTES = 4 * 1024
_HOST_RE = re.compile(r"factlane-[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.test")

_NSS_INIT_NOROOTINIT = 0x10
_SEC_SUCCESS = 0
_CK_INVALID_HANDLE = 0
_CERTDB_VALID_CA = 1 << 3
_CERTDB_TRUSTED_CA = 1 << 4
_CERTDB_NS_TRUSTED_CA = 1 << 5
_EXPECTED_SSL_CA_FLAGS = _CERTDB_VALID_CA | _CERTDB_TRUSTED_CA
_ALLOWED_SSL_CA_FLAGS = _EXPECTED_SSL_CA_FLAGS | _CERTDB_NS_TRUSTED_CA

_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_FILE_FLAG_DELETE_ON_CLOSE = 0x04000000
_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_DELETE = 0x00010000
_FILE_ATTRIBUTE_NORMAL = 0x00000080
_OPEN_EXISTING = 3
_CREATE_NEW = 1
_MOVEFILE_REPLACE_EXISTING = 0x00000001
_MOVEFILE_WRITE_THROUGH = 0x00000008
_ERROR_FILE_NOT_FOUND = 2
_ERROR_PATH_NOT_FOUND = 3
_ERROR_ACCESS_DENIED = 5
_ERROR_SHARING_VIOLATION = 32
_ERROR_LOCK_VIOLATION = 33
_ERROR_FILE_EXISTS = 80
_ERROR_ALREADY_EXISTS = 183

_NSS_LOCK = threading.Lock()


class WindowsFirefoxTrustError(RuntimeError):
    """Fail-closed Firefox profile trust provisioning failure."""

    def __init__(self, code: str, message: str, *, nss_error: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.safe_message = message
        self.nss_error = nss_error


@dataclass(frozen=True)
class WindowsFirefoxTrustReceipt:
    status: str
    firefox_executable: Path
    firefox_sha256: str
    nss_dll_path: Path
    profile_dir: Path
    root_fingerprint_sha256: str
    ssl_trust_flags: int
    db_initialized: bool


class _SECItem(ctypes.Structure):
    # SECItem from seccomon.h: SECItemType type; unsigned char *data; unsigned int len.
    _fields_ = [
        ("type", ctypes.c_uint),
        ("data", ctypes.POINTER(ctypes.c_ubyte)),
        ("len", ctypes.c_uint),
    ]


class _CERTCertTrust(ctypes.Structure):
    # CERTCertTrust from certt.h.
    _fields_ = [
        ("sslFlags", ctypes.c_uint),
        ("emailFlags", ctypes.c_uint),
        ("objectSigningFlags", ctypes.c_uint),
    ]


def _raise(
    code: str,
    message: str,
    exc: BaseException | None = None,
    *,
    nss_error: int | None = None,
) -> None:
    error = WindowsFirefoxTrustError(code, message, nss_error=nss_error)
    if exc is None:
        raise error
    raise error from exc


def _require_windows() -> tuple[Any, Any, Any]:
    if os.name != "nt":
        _raise(
            "WINDOWS_FIREFOX_TRUST_UNAVAILABLE",
            "Firefox profile trust provisioning is available only on native Windows",
        )
    if ctypes.sizeof(ctypes.c_void_p) != 8 or ctypes.sizeof(ctypes.c_ulong) != 4 or ctypes.sizeof(ctypes.c_int) != 4:
        _raise(
            "WINDOWS_FIREFOX_NSS_ABI_UNSUPPORTED",
            "Firefox NSS trust provisioning requires the native Windows x64 ABI",
        )
    try:
        import pywintypes
        import win32file
        import win32security
    except ImportError as exc:  # pragma: no cover - native packaging guard
        _raise(
            "WINDOWS_FIREFOX_TRUST_UNAVAILABLE",
            "Firefox profile trust provisioning requires pywin32",
            exc,
        )
    return pywintypes, win32file, win32security


def _norm(path: str | os.PathLike[str]) -> str:
    return ntpath.normcase(ntpath.abspath(os.fspath(path)))


def _final_path_for_handle(handle: Any) -> Path:
    try:
        raw_handle = int(handle)
    except Exception as exc:
        _raise("WINDOWS_FIREFOX_PATH_UNSAFE", "Windows path handle is invalid", exc)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_final = kernel32.GetFinalPathNameByHandleW
    get_final.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    get_final.restype = wintypes.DWORD
    needed = int(get_final(wintypes.HANDLE(raw_handle), None, 0, 0))
    if needed <= 0 or needed > 32768:
        _raise("WINDOWS_FIREFOX_PATH_UNSAFE", "Windows final path identity is unavailable")
    buffer = ctypes.create_unicode_buffer(needed + 1)
    written = int(get_final(wintypes.HANDLE(raw_handle), buffer, len(buffer), 0))
    if written <= 0 or written >= len(buffer):
        _raise("WINDOWS_FIREFOX_PATH_UNSAFE", "Windows final path identity could not be read")
    value = buffer.value
    if value.startswith("\\\\?\\UNC\\"):
        value = "\\\\" + value[8:]
    elif value.startswith("\\\\?\\"):
        value = value[4:]
    return Path(value)


class _PinnedFile:
    def __init__(self, path: Path, handle: Any) -> None:
        self.path = path
        self.handle = handle

    @classmethod
    def open(cls, path: Path) -> _PinnedFile:
        _pywintypes, win32file, _win32security = _require_windows()
        try:
            validate_windows_path_no_reparse(path)
            if not path.is_file():
                _raise("WINDOWS_FIREFOX_EXECUTABLE_INVALID", "Firefox installation file is unavailable")
            handle = win32file.CreateFile(
                str(path),
                _GENERIC_READ,
                _FILE_SHARE_READ,
                None,
                _OPEN_EXISTING,
                _FILE_ATTRIBUTE_NORMAL | _FILE_FLAG_OPEN_REPARSE_POINT,
                None,
            )
            if _norm(_final_path_for_handle(handle)) != _norm(path):
                handle.Close()
                _raise("WINDOWS_FIREFOX_PATH_UNSAFE", "Firefox installation file identity changed")
            return cls(path, handle)
        except WindowsFirefoxTrustError:
            raise
        except WindowsSecurityError as exc:
            _raise("WINDOWS_FIREFOX_PATH_UNSAFE", "Firefox installation path is unsafe", exc)
        except Exception as exc:
            _raise("WINDOWS_FIREFOX_EXECUTABLE_INVALID", "Firefox installation file could not be pinned", exc)

    def sha256(self) -> str:
        try:
            return hashlib.sha256(self.path.read_bytes()).hexdigest()
        except OSError as exc:
            _raise("WINDOWS_FIREFOX_EXECUTABLE_INVALID", "Firefox executable identity could not be hashed", exc)

    def close(self) -> None:
        handle, self.handle = self.handle, None
        if handle is not None:
            handle.Close()

    def __enter__(self) -> _PinnedFile:
        return self

    def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        self.close()


class _ProfileMutationGuard:
    def __init__(self, profile: Path, owner_sid: str, directory_handle: Any, lock_handle: Any) -> None:
        self.profile = profile
        self.owner_sid = owner_sid
        self.directory_handle = directory_handle
        self.lock_handle = lock_handle

    @classmethod
    def open(cls, profile: Path, owner_sid: str) -> _ProfileMutationGuard:
        pywintypes, win32file, _win32security = _require_windows()
        try:
            ensure_owner_private_directory(profile, owner_sid=owner_sid)
            validate_owner_private_path(profile, owner_sid=owner_sid, directory=True)
            directory_handle = win32file.CreateFile(
                str(profile),
                0,
                _FILE_SHARE_READ | _FILE_SHARE_WRITE,
                None,
                _OPEN_EXISTING,
                _FILE_FLAG_OPEN_REPARSE_POINT | _FILE_FLAG_BACKUP_SEMANTICS,
                None,
            )
        except WindowsSecurityError as exc:
            _raise("WINDOWS_FIREFOX_PROFILE_UNSAFE", "Firefox profile is not owner-private local state", exc)
        except Exception as exc:
            _raise("WINDOWS_FIREFOX_PROFILE_UNSAFE", "Firefox profile directory could not be pinned", exc)
        guard: _ProfileMutationGuard | None = None
        try:
            guard = cls(profile, owner_sid, directory_handle, None)
            guard.revalidate()
            guard.lock_handle = guard._acquire_firefox_profile_lock(pywintypes, win32file)
            guard.revalidate()
            return guard
        except BaseException:
            if guard is not None:
                guard.close()
            else:
                directory_handle.Close()
            raise

    def _acquire_firefox_profile_lock(self, pywintypes: Any, win32file: Any) -> Any:
        lock_path = self.profile / "parent.lock"
        for _attempt in range(4):
            self.revalidate()
            try:
                validate_windows_path_no_reparse(lock_path)
                return win32file.CreateFile(
                    str(lock_path),
                    _GENERIC_READ,
                    0,
                    None,
                    _OPEN_EXISTING,
                    _FILE_ATTRIBUTE_NORMAL | _FILE_FLAG_OPEN_REPARSE_POINT,
                    None,
                )
            except WindowsSecurityError as exc:
                _raise("WINDOWS_FIREFOX_PROFILE_UNSAFE", "Firefox profile lock path is unsafe", exc)
            except pywintypes.error as exc:
                winerror = int(getattr(exc, "winerror", 0) or 0)
                if winerror in {_ERROR_SHARING_VIOLATION, _ERROR_LOCK_VIOLATION, _ERROR_ACCESS_DENIED}:
                    _raise(
                        "WINDOWS_FIREFOX_PROFILE_LOCKED",
                        "Firefox profile is currently locked by another process",
                        exc,
                    )
                if winerror not in {_ERROR_FILE_NOT_FOUND, _ERROR_PATH_NOT_FOUND}:
                    _raise("WINDOWS_FIREFOX_PROFILE_UNSAFE", "Firefox profile lock could not be inspected", exc)
            try:
                return win32file.CreateFile(
                    str(lock_path),
                    _GENERIC_READ | _DELETE,
                    0,
                    owner_only_security_attributes(self.owner_sid),
                    _CREATE_NEW,
                    _FILE_ATTRIBUTE_NORMAL | _FILE_FLAG_OPEN_REPARSE_POINT | _FILE_FLAG_DELETE_ON_CLOSE,
                    None,
                )
            except pywintypes.error as exc:
                winerror = int(getattr(exc, "winerror", 0) or 0)
                if winerror in {_ERROR_FILE_EXISTS, _ERROR_ALREADY_EXISTS}:
                    continue
                if winerror in {_ERROR_SHARING_VIOLATION, _ERROR_LOCK_VIOLATION, _ERROR_ACCESS_DENIED}:
                    _raise(
                        "WINDOWS_FIREFOX_PROFILE_LOCKED",
                        "Firefox profile became locked by another process",
                        exc,
                    )
                _raise("WINDOWS_FIREFOX_PROFILE_UNSAFE", "Firefox profile lock could not be acquired", exc)
        _raise("WINDOWS_FIREFOX_PROFILE_LOCKED", "Firefox profile lock could not be acquired without a race")

    def revalidate(self) -> None:
        try:
            validate_windows_path_no_reparse(self.profile)
            validate_owner_private_path(self.profile, owner_sid=self.owner_sid, directory=True)
            if _norm(_final_path_for_handle(self.directory_handle)) != _norm(self.profile):
                _raise("WINDOWS_FIREFOX_PROFILE_UNSAFE", "Firefox profile directory identity changed")
        except WindowsFirefoxTrustError:
            raise
        except WindowsSecurityError as exc:
            _raise("WINDOWS_FIREFOX_PROFILE_UNSAFE", "Firefox profile custody validation failed", exc)

    def close(self) -> None:
        lock_handle, self.lock_handle = self.lock_handle, None
        if lock_handle is not None:
            lock_handle.Close()
        directory_handle, self.directory_handle = self.directory_handle, None
        if directory_handle is not None:
            directory_handle.Close()

    def __enter__(self) -> _ProfileMutationGuard:
        return self

    def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        self.close()


class _NSS:
    def __init__(self, dll: Any) -> None:
        self.dll = dll
        self._bind()

    def _bind(self) -> None:
        dll = self.dll
        # nss.h
        dll.NSS_Initialize.argtypes = [
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_uint32,
        ]
        dll.NSS_Initialize.restype = ctypes.c_int
        dll.NSS_IsInitialized.argtypes = []
        dll.NSS_IsInitialized.restype = ctypes.c_int
        dll.NSS_Shutdown.argtypes = []
        dll.NSS_Shutdown.restype = ctypes.c_int
        dll.PORT_GetError.argtypes = []
        dll.PORT_GetError.restype = ctypes.c_int
        # cert.h / certt.h
        dll.CERT_GetDefaultCertDB.argtypes = []
        dll.CERT_GetDefaultCertDB.restype = ctypes.c_void_p
        dll.CERT_NewTempCertificate.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(_SECItem),
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_int,
        ]
        dll.CERT_NewTempCertificate.restype = ctypes.c_void_p
        dll.CERT_FindCertByDERCert.argtypes = [ctypes.c_void_p, ctypes.POINTER(_SECItem)]
        dll.CERT_FindCertByDERCert.restype = ctypes.c_void_p
        dll.CERT_FindCertByNickname.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        dll.CERT_FindCertByNickname.restype = ctypes.c_void_p
        dll.CERT_CompareCerts.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        dll.CERT_CompareCerts.restype = ctypes.c_int
        dll.CERT_GetCertTrust.argtypes = [ctypes.c_void_p, ctypes.POINTER(_CERTCertTrust)]
        dll.CERT_GetCertTrust.restype = ctypes.c_int
        dll.CERT_ChangeCertTrust.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(_CERTCertTrust)]
        dll.CERT_ChangeCertTrust.restype = ctypes.c_int
        dll.CERT_GetCertificateDer.argtypes = [ctypes.c_void_p, ctypes.POINTER(_SECItem)]
        dll.CERT_GetCertificateDer.restype = ctypes.c_int
        dll.SEC_DeletePermCertificate.argtypes = [ctypes.c_void_p]
        dll.SEC_DeletePermCertificate.restype = ctypes.c_int
        dll.CERT_DestroyCertificate.argtypes = [ctypes.c_void_p]
        dll.CERT_DestroyCertificate.restype = None
        # pk11pub.h. CK_OBJECT_HANDLE is CK_ULONG, which is 32-bit on Windows x64.
        dll.PK11_GetInternalKeySlot.argtypes = []
        dll.PK11_GetInternalKeySlot.restype = ctypes.c_void_p
        dll.PK11_NeedUserInit.argtypes = [ctypes.c_void_p]
        dll.PK11_NeedUserInit.restype = ctypes.c_int
        dll.PK11_InitPin.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p]
        dll.PK11_InitPin.restype = ctypes.c_int
        dll.PK11_Authenticate.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
        dll.PK11_Authenticate.restype = ctypes.c_int
        dll.PK11_ImportCert.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.c_char_p,
            ctypes.c_int,
        ]
        dll.PK11_ImportCert.restype = ctypes.c_int
        dll.PK11_FreeSlot.argtypes = [ctypes.c_void_p]
        dll.PK11_FreeSlot.restype = None

    def error(self) -> int:
        return int(self.dll.PORT_GetError())

    def require(self, value: int, code: str, message: str) -> None:
        if int(value) != _SEC_SUCCESS:
            _raise(code, message, nss_error=self.error())


def _loaded_module_path(module_handle: int) -> Path:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_name = kernel32.GetModuleFileNameW
    get_name.argtypes = [wintypes.HMODULE, wintypes.LPWSTR, wintypes.DWORD]
    get_name.restype = wintypes.DWORD
    buffer = ctypes.create_unicode_buffer(32768)
    written = int(get_name(wintypes.HMODULE(module_handle), buffer, len(buffer)))
    if written <= 0 or written >= len(buffer):
        _raise("WINDOWS_FIREFOX_NSS_INVALID", "Loaded Firefox NSS module identity is unavailable")
    return Path(buffer.value)


def _load_nss(nss_path: Path) -> tuple[_NSS, Any]:
    _require_windows()
    add_directory = getattr(os, "add_dll_directory", None)
    if not callable(add_directory):
        _raise("WINDOWS_FIREFOX_NSS_INVALID", "Scoped Windows DLL loading is unavailable")
    scope = None
    try:
        scope = add_directory(str(nss_path.parent))
        dll = ctypes.WinDLL(str(nss_path))
        if _norm(_loaded_module_path(int(dll._handle))) != _norm(nss_path):
            _raise("WINDOWS_FIREFOX_NSS_INVALID", "Loaded NSS library is not the Firefox-shipped nss3.dll")
        required = (
            "NSS_Initialize",
            "NSS_IsInitialized",
            "NSS_Shutdown",
            "PORT_GetError",
            "CERT_GetDefaultCertDB",
            "CERT_NewTempCertificate",
            "CERT_FindCertByDERCert",
            "CERT_FindCertByNickname",
            "CERT_CompareCerts",
            "CERT_GetCertTrust",
            "CERT_ChangeCertTrust",
            "CERT_GetCertificateDer",
            "SEC_DeletePermCertificate",
            "CERT_DestroyCertificate",
            "PK11_GetInternalKeySlot",
            "PK11_NeedUserInit",
            "PK11_InitPin",
            "PK11_Authenticate",
            "PK11_ImportCert",
            "PK11_FreeSlot",
        )
        if any(not hasattr(dll, name) for name in required):
            _raise("WINDOWS_FIREFOX_NSS_INVALID", "Firefox NSS library is missing a required public API")
        return _NSS(dll), scope
    except WindowsFirefoxTrustError:
        if scope is not None:
            scope.close()
        raise
    except Exception as exc:
        if scope is not None:
            scope.close()
        _raise("WINDOWS_FIREFOX_NSS_INVALID", "Firefox NSS library could not be loaded safely", exc)


def _validate_firefox_install(executable: str | os.PathLike[str]) -> tuple[Path, Path, str, _PinnedFile, _PinnedFile]:
    _require_windows()
    firefox = Path(executable)
    if firefox.name.casefold() != "firefox.exe":
        _raise("WINDOWS_FIREFOX_EXECUTABLE_INVALID", "Firefox executable must be firefox.exe")
    try:
        validate_windows_path_no_reparse(firefox)
    except WindowsSecurityError as exc:
        _raise("WINDOWS_FIREFOX_EXECUTABLE_INVALID", "Firefox executable path is unsafe", exc)
    nss_path = firefox.parent / _NSS_DLL_NAME
    try:
        validate_windows_path_no_reparse(nss_path)
    except WindowsSecurityError as exc:
        _raise("WINDOWS_FIREFOX_NSS_INVALID", "Firefox NSS path is unsafe", exc)
    firefox_pin = _PinnedFile.open(firefox)
    try:
        nss_pin = _PinnedFile.open(nss_path)
    except BaseException:
        firefox_pin.close()
        raise
    return firefox, nss_path, firefox_pin.sha256(), firefox_pin, nss_pin


def _certificate_material(material: WindowsTLSMaterial) -> tuple[WindowsTLSMaterial, bytes, bytes]:
    if not isinstance(material, WindowsTLSMaterial):
        raise TypeError("material must be WindowsTLSMaterial")
    try:
        validated = load_windows_tls_material(material.root_dir, material.hostname)
    except WindowsTLSError as exc:
        _raise("WINDOWS_FIREFOX_ROOT_INVALID", "FactLane TLS material is not valid current Windows TLS state", exc)
    if validated.root_fingerprint_sha256 != material.root_fingerprint_sha256:
        _raise("WINDOWS_FIREFOX_ROOT_INVALID", "FactLane TLS root identity changed before Firefox trust provisioning")
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization

        root = x509.load_pem_x509_certificate(validated.root_cert_path.read_bytes())
        leaf = x509.load_pem_x509_certificate(validated.leaf_cert_path.read_bytes())
        root_bc = root.extensions.get_extension_for_class(x509.BasicConstraints).value
        root_ku = root.extensions.get_extension_for_class(x509.KeyUsage).value
        leaf_bc = leaf.extensions.get_extension_for_class(x509.BasicConstraints).value
        root_der = root.public_bytes(serialization.Encoding.DER)
        leaf_der = leaf.public_bytes(serialization.Encoding.DER)
        root_fp = root.fingerprint(hashes.SHA256()).hex()
        leaf_fp = leaf.fingerprint(hashes.SHA256()).hex()
    except Exception as exc:
        _raise("WINDOWS_FIREFOX_ROOT_INVALID", "FactLane TLS certificates could not be parsed for Firefox trust", exc)
    if (
        not root_bc.ca
        or root_bc.path_length != 0
        or not root_ku.key_cert_sign
        or not root_ku.crl_sign
        or leaf_bc.ca
        or root_fp != validated.root_fingerprint_sha256
        or leaf_fp != validated.leaf_fingerprint_sha256
        or root_fp == leaf_fp
        or hashlib.sha256(root_der).hexdigest() != root_fp
    ):
        _raise("WINDOWS_FIREFOX_ROOT_INVALID", "FactLane TLS root does not satisfy the private-CA trust contract")
    return validated, root_der, leaf_der


def _manifest_path(profile: Path) -> Path:
    return profile / _MANIFEST_NAME


def _load_manifest(profile: Path, owner_sid: str) -> dict[str, Any] | None:
    path = _manifest_path(profile)
    try:
        validate_windows_path_no_reparse(path)
    except WindowsSecurityError as exc:
        _raise("WINDOWS_FIREFOX_TRUST_MANIFEST_INVALID", "Firefox trust manifest path is unsafe", exc)
    if not path.exists():
        return None
    try:
        validate_owner_private_path(path, owner_sid=owner_sid, directory=False)
        if path.stat().st_size > _MAX_MANIFEST_BYTES:
            raise ValueError("manifest too large")
        raw = path.read_bytes()
        parsed = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError, WindowsSecurityError) as exc:
        _raise("WINDOWS_FIREFOX_TRUST_MANIFEST_INVALID", "Firefox trust manifest is invalid or not owner-private", exc)
    expected = {
        "schema_version",
        "firefox_executable",
        "firefox_sha256",
        "profile_dir",
        "root_fingerprint_sha256",
    }
    if not isinstance(parsed, dict) or set(parsed) != expected or parsed.get("schema_version") != _MANIFEST_VERSION:
        _raise("WINDOWS_FIREFOX_TRUST_MANIFEST_INVALID", "Firefox trust manifest schema is invalid")
    for field in ("firefox_executable", "firefox_sha256", "profile_dir", "root_fingerprint_sha256"):
        if not isinstance(parsed.get(field), str) or not parsed[field] or len(parsed[field]) > 32768:
            _raise("WINDOWS_FIREFOX_TRUST_MANIFEST_INVALID", "Firefox trust manifest binding is invalid")
    if len(parsed["firefox_sha256"]) != 64 or len(parsed["root_fingerprint_sha256"]) != 64:
        _raise("WINDOWS_FIREFOX_TRUST_MANIFEST_INVALID", "Firefox trust manifest fingerprint is invalid")
    return parsed


def _validate_manifest_identity(
    manifest: dict[str, Any],
    *,
    firefox: Path,
    firefox_sha256: str,
    profile: Path,
) -> None:
    if _norm(manifest["profile_dir"]) != _norm(profile):
        _raise("WINDOWS_FIREFOX_TRUST_PROFILE_MISMATCH", "Firefox trust manifest is bound to a different profile")
    if _norm(manifest["firefox_executable"]) != _norm(firefox) or manifest["firefox_sha256"] != firefox_sha256:
        _raise(
            "WINDOWS_FIREFOX_TRUST_EXECUTABLE_MISMATCH",
            "Firefox trust manifest is bound to a different Firefox executable identity",
        )
def _manifest_bytes(*, firefox: Path, firefox_sha256: str, profile: Path, root_fingerprint: str) -> bytes:
    return (
        json.dumps(
            {
                "schema_version": _MANIFEST_VERSION,
                "firefox_executable": str(firefox),
                "firefox_sha256": firefox_sha256,
                "profile_dir": str(profile),
                "root_fingerprint_sha256": root_fingerprint,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _write_profile_file_atomic(
    guard: _ProfileMutationGuard,
    *,
    target_name: str,
    data: bytes,
    failure_code: str,
    error_message: str,
    replace_existing: bool = True,
) -> None:
    if Path(target_name).name != target_name or not target_name or "\\" in target_name or "/" in target_name:
        raise ValueError("Firefox profile target must be one basename")
    pywintypes, win32file, _win32security = _require_windows()
    target = guard.profile / target_name
    temporary = guard.profile / f".{target_name}.{secrets.token_hex(16)}.tmp"
    handle = None
    try:
        guard.revalidate()
        handle = win32file.CreateFile(
            str(temporary),
            _GENERIC_WRITE,
            0,
            owner_only_security_attributes(guard.owner_sid),
            _CREATE_NEW,
            _FILE_ATTRIBUTE_NORMAL | _FILE_FLAG_OPEN_REPARSE_POINT,
            None,
        )
        write_error, written = win32file.WriteFile(handle, data)
        if int(write_error) != 0 or int(written) != len(data):
            _raise(failure_code, error_message)
        win32file.FlushFileBuffers(handle)
        handle.Close()
        handle = None
        validate_owner_private_path(temporary, owner_sid=guard.owner_sid, directory=False)
        guard.revalidate()
        if target.exists():
            validate_owner_private_path(target, owner_sid=guard.owner_sid, directory=False)
        move_flags = _MOVEFILE_WRITE_THROUGH
        if replace_existing:
            move_flags |= _MOVEFILE_REPLACE_EXISTING
        win32file.MoveFileEx(
            str(temporary),
            str(target),
            move_flags,
        )
        validate_owner_private_path(target, owner_sid=guard.owner_sid, directory=False)
    except WindowsFirefoxTrustError:
        raise
    except (WindowsSecurityError, pywintypes.error, OSError) as exc:
        _raise(failure_code, error_message, exc)
    finally:
        if handle is not None:
            handle.Close()
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


def _write_manifest_atomic(guard: _ProfileMutationGuard, data: bytes) -> None:
    _write_profile_file_atomic(
        guard,
        target_name=_MANIFEST_NAME,
        data=data,
        failure_code="WINDOWS_FIREFOX_MANIFEST_WRITE_FAILED",
        error_message="Firefox trust manifest could not be published safely",
    )


def _profile_network_policy_bytes(hostname: str) -> bytes:
    if not isinstance(hostname, str) or _HOST_RE.fullmatch(hostname) is None:
        raise ValueError("hostname must be one canonical factlane-<id>.test name")
    return (
        "// FactLane managed Firefox profile network policy v1\n"
        f'user_pref("network.dns.localDomains", "{hostname}");\n'
        'user_pref("network.proxy.type", 0);\n'
        'user_pref("network.trr.mode", 5);\n'
    ).encode("utf-8")


def ensure_firefox_profile_network_policy(
    *,
    profile_dir: str | os.PathLike[str],
    hostname: str,
    owner_sid: str | None = None,
) -> Path:
    """Bind one isolated Firefox profile to the exact local FactLane hostname."""

    _require_windows()
    sid = current_user_sid() if owner_sid is None else owner_sid
    profile = Path(profile_dir)
    expected = _profile_network_policy_bytes(hostname)
    target = profile / _PROFILE_PREFS_NAME
    with _ProfileMutationGuard.open(profile, sid) as guard:
        try:
            validate_windows_path_no_reparse(target)
        except WindowsSecurityError as exc:
            _raise("WINDOWS_FIREFOX_PROFILE_PREFS_UNSAFE", "Firefox profile network policy path is unsafe", exc)
        if target.exists():
            try:
                validate_owner_private_path(target, owner_sid=sid, directory=False)
                if target.stat().st_size > _MAX_PROFILE_PREFS_BYTES:
                    raise ValueError("profile preferences too large")
                current = target.read_bytes()
            except (OSError, ValueError, WindowsSecurityError) as exc:
                _raise(
                    "WINDOWS_FIREFOX_PROFILE_PREFS_UNSAFE",
                    "Firefox profile network policy is invalid or not owner-private",
                    exc,
                )
            if current != expected:
                _raise(
                    "WINDOWS_FIREFOX_PROFILE_PREFS_CONFLICT",
                    "Firefox isolated profile contains unmanaged or conflicting user.js preferences",
                )
            return target
        _write_profile_file_atomic(
            guard,
            target_name=_PROFILE_PREFS_NAME,
            data=expected,
            failure_code="WINDOWS_FIREFOX_PROFILE_PREFS_WRITE_FAILED",
            error_message="Firefox profile network policy could not be published safely",
            replace_existing=False,
        )
        return target


def _security_db_state(profile: Path, owner_sid: str) -> bool:
    paths = [profile / name for name in _NSS_DB_FILES]
    existing = [path.exists() for path in paths]
    if not any(existing):
        return False
    if not all(existing):
        _raise("WINDOWS_FIREFOX_PROFILE_DB_INVALID", "Firefox NSS profile database is incomplete")
    try:
        for path in paths:
            validate_owner_private_path(path, owner_sid=owner_sid, directory=False)
        with (profile / "cert9.db").open("rb") as handle:
            cert_header = handle.read(16)
        with (profile / "key4.db").open("rb") as handle:
            key_header = handle.read(16)
        if cert_header != b"SQLite format 3\x00":
            raise ValueError("cert9.db header")
        if key_header != b"SQLite format 3\x00":
            raise ValueError("key4.db header")
    except (OSError, ValueError, WindowsSecurityError) as exc:
        _raise("WINDOWS_FIREFOX_PROFILE_DB_INVALID", "Firefox NSS profile database is corrupt or unsafe", exc)
    return True


def _precreate_fresh_db_files(guard: _ProfileMutationGuard) -> None:
    pywintypes, win32file, _win32security = _require_windows()
    created: list[Path] = []
    try:
        for name in _NSS_DB_FILES:
            guard.revalidate()
            path = guard.profile / name
            try:
                handle = win32file.CreateFile(
                    str(path),
                    _GENERIC_WRITE,
                    0,
                    owner_only_security_attributes(guard.owner_sid),
                    _CREATE_NEW,
                    _FILE_ATTRIBUTE_NORMAL | _FILE_FLAG_OPEN_REPARSE_POINT,
                    None,
                )
            except pywintypes.error as exc:
                winerror = int(getattr(exc, "winerror", 0) or 0)
                if winerror in {_ERROR_FILE_EXISTS, _ERROR_ALREADY_EXISTS}:
                    _raise(
                        "WINDOWS_FIREFOX_PROFILE_DB_RACE",
                        "Firefox NSS database appeared during fresh owner-private initialization",
                        exc,
                    )
                _raise(
                    "WINDOWS_FIREFOX_PROFILE_DB_INVALID",
                    "Firefox NSS database file could not be created owner-private",
                    exc,
                )
            created.append(path)
            try:
                win32file.FlushFileBuffers(handle)
            finally:
                handle.Close()
            validate_owner_private_path(path, owner_sid=guard.owner_sid, directory=False)
        guard.revalidate()
    except WindowsFirefoxTrustError:
        for path in reversed(created):
            try:
                path.unlink()
            except OSError:
                pass
        raise
    except (WindowsSecurityError, pywintypes.error, OSError) as exc:
        for path in reversed(created):
            try:
                path.unlink()
            except OSError:
                pass
        _raise(
            "WINDOWS_FIREFOX_PROFILE_DB_INVALID",
            "Fresh Firefox NSS database could not be created as owner-private state",
            exc,
        )


def _cleanup_fresh_db_files(guard: _ProfileMutationGuard, *, remove_manifest: bool) -> None:
    names = [
        "cert9.db-wal",
        "cert9.db-shm",
        "cert9.db-journal",
        "key4.db-wal",
        "key4.db-shm",
        "key4.db-journal",
        *_NSS_DB_FILES,
    ]
    if remove_manifest:
        names.append(_MANIFEST_NAME)
    failures: list[str] = []
    guard.revalidate()
    for name in names:
        path = guard.profile / name
        try:
            validate_windows_path_no_reparse(path)
            path.unlink(missing_ok=True)
        except (OSError, WindowsSecurityError):
            failures.append(name)
    guard.revalidate()
    if failures:
        _raise(
            "WINDOWS_FIREFOX_PROFILE_ROLLBACK_FAILED",
            "Fresh Firefox NSS profile initialization could not be rolled back completely",
        )


def _snapshot_unrelated(profile: Path) -> dict[str, tuple[bool, int, int, int]]:
    allowed = {*_NSS_DB_FILES, _MANIFEST_NAME, "parent.lock"}
    result: dict[str, tuple[bool, int, int, int]] = {}
    try:
        for path in profile.iterdir():
            name = path.name
            if name in allowed or name.startswith("cert9.db-") or name.startswith("key4.db-") or name.startswith(f".{_MANIFEST_NAME}."):
                continue
            info = path.lstat()
            result[name] = (
                path.is_dir(),
                int(info.st_size),
                int(info.st_mtime_ns),
                int(getattr(info, "st_file_attributes", 0)),
            )
        return result
    except OSError as exc:
        _raise("WINDOWS_FIREFOX_PROFILE_UNSAFE", "Firefox profile state could not be snapshotted safely", exc)


def _sec_item(data: bytes) -> tuple[_SECItem, Any]:
    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    item = _SECItem(0, ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)), len(data))
    return item, buffer


def _cert_trust(nss: _NSS, cert: int) -> _CERTCertTrust:
    trust = _CERTCertTrust()
    nss.require(
        nss.dll.CERT_GetCertTrust(cert, ctypes.byref(trust)),
        "WINDOWS_FIREFOX_NSS_OPERATION_FAILED",
        "Firefox NSS certificate trust could not be read",
    )
    return trust


def _require_root_trust(trust: _CERTCertTrust) -> int:
    ssl_flags = int(trust.sslFlags)
    if (
        ssl_flags & _EXPECTED_SSL_CA_FLAGS != _EXPECTED_SSL_CA_FLAGS
        or ssl_flags & ~_ALLOWED_SSL_CA_FLAGS
        or int(trust.emailFlags) != 0
        or int(trust.objectSigningFlags) != 0
    ):
        _raise(
            "WINDOWS_FIREFOX_TRUST_STATE_INVALID",
            "Firefox root trust is not limited to the required SSL CA role",
        )
    return ssl_flags


def _require_leaf_untrusted(nss: _NSS, db: int, leaf_item: _SECItem) -> None:
    leaf = nss.dll.CERT_FindCertByDERCert(db, ctypes.byref(leaf_item))
    if not leaf:
        return
    try:
        trust = _cert_trust(nss, leaf)
        if int(trust.sslFlags) != 0 or int(trust.emailFlags) != 0 or int(trust.objectSigningFlags) != 0:
            _raise("WINDOWS_FIREFOX_TRUST_STATE_INVALID", "Firefox profile contains explicit trust for the FactLane leaf")
    finally:
        nss.dll.CERT_DestroyCertificate(leaf)


def _certificate_der(nss: _NSS, cert: int) -> bytes:
    der = _SECItem()
    nss.require(
        nss.dll.CERT_GetCertificateDer(cert, ctypes.byref(der)),
        "WINDOWS_FIREFOX_NSS_OPERATION_FAILED",
        "Firefox NSS certificate encoding could not be read",
    )
    if not der.data or int(der.len) <= 0 or int(der.len) > 1024 * 1024:
        _raise(
            "WINDOWS_FIREFOX_TRUST_STATE_INVALID",
            "Firefox NSS certificate encoding is invalid",
        )
    return ctypes.string_at(der.data, int(der.len))


def _delete_permanent_certificate(nss: _NSS, cert: int) -> None:
    nss.require(
        nss.dll.SEC_DeletePermCertificate(cert),
        "WINDOWS_FIREFOX_NSS_OPERATION_FAILED",
        "FactLane-owned Firefox CA could not be removed for managed rotation",
    )


def _import_root_certificate(
    *,
    nss: _NSS,
    db: int,
    slot: int,
    root_der: bytes,
    requested_ssl_flags: int,
) -> int:
    root_item, root_buffer = _sec_item(root_der)
    _backing_buffer = root_buffer
    temp = nss.dll.CERT_NewTempCertificate(db, ctypes.byref(root_item), None, 0, 1)
    if not temp:
        _raise(
            "WINDOWS_FIREFOX_NSS_OPERATION_FAILED",
            "FactLane private CA could not be decoded by Firefox NSS",
            nss_error=nss.error(),
        )
    try:
        nss.require(
            nss.dll.PK11_ImportCert(slot, temp, _CK_INVALID_HANDLE, _FACTLANE_CA_NICKNAME, 0),
            "WINDOWS_FIREFOX_NSS_OPERATION_FAILED",
            "FactLane private CA could not be imported into the Firefox profile",
        )
        requested = _CERTCertTrust(requested_ssl_flags, 0, 0)
        nss.require(
            nss.dll.CERT_ChangeCertTrust(db, temp, ctypes.byref(requested)),
            "WINDOWS_FIREFOX_NSS_OPERATION_FAILED",
            "Firefox NSS SSL CA trust could not be assigned",
        )
    finally:
        nss.dll.CERT_DestroyCertificate(temp)

    verified = nss.dll.CERT_FindCertByDERCert(db, ctypes.byref(root_item))
    if not verified:
        _raise(
            "WINDOWS_FIREFOX_TRUST_STATE_INVALID",
            "Imported FactLane private CA is not present in the Firefox NSS database",
            nss_error=nss.error(),
        )
    try:
        return _require_root_trust(_cert_trust(nss, verified))
    finally:
        nss.dll.CERT_DestroyCertificate(verified)


def _provision_nss_trust(
    *,
    nss: _NSS,
    profile: Path,
    root_der: bytes,
    leaf_der: bytes,
    manifest_exists: bool,
    db_preexisting: bool,
    previous_root_fingerprint: str | None = None,
    commit_rebind: Callable[[], None] | None = None,
    rollback_rebind: Callable[[], None] | None = None,
) -> tuple[str, int]:
    root_item, root_buffer = _sec_item(root_der)
    leaf_item, leaf_buffer = _sec_item(leaf_der)
    # Keep the ctypes arrays alive for the full NSS call sequence because the
    # SECItem structures contain raw pointers into these buffers.
    _backing_buffers = (root_buffer, leaf_buffer)
    config = ("sql:" + str(profile)).encode("utf-8")
    initialized = False
    primary_error: BaseException | None = None
    try:
        if nss.dll.NSS_IsInitialized():
            _raise(
                "WINDOWS_FIREFOX_NSS_BUSY",
                "Firefox NSS is already initialized in this process",
            )
        nss.require(
            nss.dll.NSS_Initialize(config, b"", b"", b"secmod.db", _NSS_INIT_NOROOTINIT),
            "WINDOWS_FIREFOX_NSS_INIT_FAILED",
            "Firefox NSS SQL profile could not be initialized",
        )
        initialized = True
        db = nss.dll.CERT_GetDefaultCertDB()
        if not db:
            _raise(
                "WINDOWS_FIREFOX_NSS_OPERATION_FAILED",
                "Firefox NSS default certificate database is unavailable",
                nss_error=nss.error(),
            )
        _require_leaf_untrusted(nss, db, leaf_item)

        by_der = nss.dll.CERT_FindCertByDERCert(db, ctypes.byref(root_item))
        by_name = nss.dll.CERT_FindCertByNickname(db, _FACTLANE_CA_NICKNAME)
        try:
            rebind_old_der: bytes | None = None
            rebind_old_ssl_flags: int | None = None
            if by_name and (not by_der or not nss.dll.CERT_CompareCerts(by_name, by_der)):
                if (
                    not manifest_exists
                    or previous_root_fingerprint is None
                    or commit_rebind is None
                    or rollback_rebind is None
                    or by_der
                ):
                    _raise(
                        "WINDOWS_FIREFOX_TRUST_ROOT_MISMATCH",
                        "Firefox profile already contains a different FactLane private CA",
                    )
                rebind_old_der = _certificate_der(nss, by_name)
                if hashlib.sha256(rebind_old_der).hexdigest() != previous_root_fingerprint:
                    _raise(
                        "WINDOWS_FIREFOX_TRUST_ROOT_MISMATCH",
                        "Firefox profile FactLane CA is not the manifest-owned previous root",
                    )
                rebind_old_ssl_flags = _require_root_trust(_cert_trust(nss, by_name))
            if by_der and not by_name:
                _raise(
                    "WINDOWS_FIREFOX_TRUST_STATE_INVALID",
                    "Firefox profile contains the supplied CA outside FactLane trust ownership",
                )
            if by_der:
                trust = _cert_trust(nss, by_der)
                try:
                    flags = _require_root_trust(trust)
                    return "REUSED", flags
                except WindowsFirefoxTrustError:
                    if manifest_exists:
                        raise

            slot = nss.dll.PK11_GetInternalKeySlot()
            if not slot:
                _raise(
                    "WINDOWS_FIREFOX_NSS_OPERATION_FAILED",
                    "Firefox NSS internal key slot is unavailable",
                    nss_error=nss.error(),
                )
            try:
                needs_init = bool(nss.dll.PK11_NeedUserInit(slot))
                if needs_init:
                    if db_preexisting:
                        _raise(
                            "WINDOWS_FIREFOX_PROFILE_DB_INVALID",
                            "Existing Firefox NSS token requires unexpected user initialization",
                        )
                    nss.require(
                        nss.dll.PK11_InitPin(slot, None, None),
                        "WINDOWS_FIREFOX_NSS_OPERATION_FAILED",
                        "Fresh Firefox NSS token could not be initialized locally",
                    )
                nss.require(
                    nss.dll.PK11_Authenticate(slot, 1, None),
                    "WINDOWS_FIREFOX_NSS_OPERATION_FAILED",
                    "Firefox NSS token could not be authenticated for profile-local trust mutation",
                )
                if rebind_old_der is not None:
                    assert by_name
                    assert rebind_old_ssl_flags is not None
                    assert commit_rebind is not None
                    assert rollback_rebind is not None
                    _delete_permanent_certificate(nss, by_name)
                    try:
                        flags = _import_root_certificate(
                            nss=nss,
                            db=db,
                            slot=slot,
                            root_der=root_der,
                            requested_ssl_flags=_EXPECTED_SSL_CA_FLAGS,
                        )
                        _require_leaf_untrusted(nss, db, leaf_item)
                        commit_rebind()
                        return "REBOUND", flags
                    except BaseException:
                        try:
                            current = nss.dll.CERT_FindCertByNickname(db, _FACTLANE_CA_NICKNAME)
                            if current:
                                try:
                                    current_der = _certificate_der(nss, current)
                                    if hashlib.sha256(current_der).digest() != hashlib.sha256(root_der).digest():
                                        _raise(
                                            "WINDOWS_FIREFOX_TRUST_REBIND_ROLLBACK_FAILED",
                                            "Firefox CA rotation rollback found an unexpected FactLane nickname binding",
                                        )
                                    _delete_permanent_certificate(nss, current)
                                finally:
                                    nss.dll.CERT_DestroyCertificate(current)
                            restored_flags = _import_root_certificate(
                                nss=nss,
                                db=db,
                                slot=slot,
                                root_der=rebind_old_der,
                                requested_ssl_flags=rebind_old_ssl_flags,
                            )
                            if restored_flags != rebind_old_ssl_flags:
                                _raise(
                                    "WINDOWS_FIREFOX_TRUST_REBIND_ROLLBACK_FAILED",
                                    "Firefox CA rotation rollback did not restore the previous trust flags",
                                )
                            rollback_rebind()
                        except BaseException as rollback_exc:
                            _raise(
                                "WINDOWS_FIREFOX_TRUST_REBIND_ROLLBACK_FAILED",
                                "Firefox CA rotation could not restore the previous managed trust binding",
                                rollback_exc,
                            )
                        raise
                if not by_der:
                    flags = _import_root_certificate(
                        nss=nss,
                        db=db,
                        slot=slot,
                        root_der=root_der,
                        requested_ssl_flags=_EXPECTED_SSL_CA_FLAGS,
                    )
                else:
                    requested = _CERTCertTrust(_EXPECTED_SSL_CA_FLAGS, 0, 0)
                    nss.require(
                        nss.dll.CERT_ChangeCertTrust(db, by_der, ctypes.byref(requested)),
                        "WINDOWS_FIREFOX_NSS_OPERATION_FAILED",
                        "Firefox NSS SSL CA trust could not be repaired before ownership binding",
                    )
                    flags = _require_root_trust(_cert_trust(nss, by_der))
            finally:
                nss.dll.PK11_FreeSlot(slot)
            _require_leaf_untrusted(nss, db, leaf_item)
            return "IMPORTED", flags
        finally:
            if by_name:
                nss.dll.CERT_DestroyCertificate(by_name)
            if by_der:
                nss.dll.CERT_DestroyCertificate(by_der)
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        if initialized:
            shutdown = int(nss.dll.NSS_Shutdown())
            if shutdown != _SEC_SUCCESS and primary_error is None:
                _raise(
                    "WINDOWS_FIREFOX_NSS_SHUTDOWN_FAILED",
                    "Firefox NSS could not shut down cleanly after profile trust provisioning",
                    nss_error=nss.error(),
                )
            if shutdown == _SEC_SUCCESS and nss.dll.NSS_IsInitialized() and primary_error is None:
                _raise(
                    "WINDOWS_FIREFOX_NSS_SHUTDOWN_FAILED",
                    "Firefox NSS remained initialized after profile trust provisioning",
                )


def ensure_firefox_profile_trust(
    *,
    firefox_executable: str | os.PathLike[str],
    profile_dir: str | os.PathLike[str],
    tls_material: WindowsTLSMaterial,
    owner_sid: str | None = None,
) -> WindowsFirefoxTrustReceipt:
    """Trust one FactLane private CA only inside one isolated Firefox profile.

    The function uses the target Firefox installation's own NSS library and its
    SQL profile database. It does not modify Windows trust stores or Firefox
    global/shared profiles.
    """

    _require_windows()
    sid = current_user_sid() if owner_sid is None else owner_sid
    material, root_der, leaf_der = _certificate_material(tls_material)
    firefox, nss_path, firefox_sha256, firefox_pin, nss_pin = _validate_firefox_install(firefox_executable)
    profile = Path(profile_dir)
    try:
        with _ProfileMutationGuard.open(profile, sid) as guard:
            before = _snapshot_unrelated(profile)
            manifest = _load_manifest(profile, sid)
            previous_root_fingerprint: str | None = None
            rebind_required = False
            if manifest is not None:
                _validate_manifest_identity(
                    manifest,
                    firefox=firefox,
                    firefox_sha256=firefox_sha256,
                    profile=profile,
                )
                previous_root_fingerprint = manifest["root_fingerprint_sha256"]
                rebind_required = previous_root_fingerprint != material.root_fingerprint_sha256
            db_preexisting = _security_db_state(profile, sid)
            fresh_db_owned = not db_preexisting
            completed = False
            if fresh_db_owned:
                _precreate_fresh_db_files(guard)
            try:
                with _NSS_LOCK:
                    nss, dll_scope = _load_nss(nss_path)
                    try:
                        commit_rebind: Callable[[], None] | None = None
                        rollback_rebind: Callable[[], None] | None = None
                        if manifest is not None and rebind_required:
                            current_manifest = _manifest_bytes(
                                firefox=firefox,
                                firefox_sha256=firefox_sha256,
                                profile=profile,
                                root_fingerprint=material.root_fingerprint_sha256,
                            )
                            previous_manifest = _manifest_bytes(
                                firefox=firefox,
                                firefox_sha256=firefox_sha256,
                                profile=profile,
                                root_fingerprint=previous_root_fingerprint,
                            )

                            def commit_rebind() -> None:
                                _write_manifest_atomic(guard, current_manifest)

                            def rollback_rebind() -> None:
                                _write_manifest_atomic(guard, previous_manifest)

                        status, ssl_flags = _provision_nss_trust(
                            nss=nss,
                            profile=profile,
                            root_der=root_der,
                            leaf_der=leaf_der,
                            manifest_exists=manifest is not None,
                            db_preexisting=db_preexisting,
                            previous_root_fingerprint=previous_root_fingerprint,
                            commit_rebind=commit_rebind,
                            rollback_rebind=rollback_rebind,
                        )
                    finally:
                        dll_scope.close()
                _security_db_state(profile, sid)
                guard.revalidate()
                if manifest is None:
                    _write_manifest_atomic(
                        guard,
                        _manifest_bytes(
                            firefox=firefox,
                            firefox_sha256=firefox_sha256,
                            profile=profile,
                            root_fingerprint=material.root_fingerprint_sha256,
                        ),
                    )
                elif not rebind_required:
                    validate_owner_private_path(_manifest_path(profile), owner_sid=sid, directory=False)
                else:
                    if status != "REBOUND":
                        _raise(
                            "WINDOWS_FIREFOX_TRUST_STATE_INVALID",
                            "Firefox trust root changed without completing the managed profile rebind",
                        )
                    rebound_manifest = _load_manifest(profile, sid)
                    if rebound_manifest is None:
                        _raise(
                            "WINDOWS_FIREFOX_TRUST_MANIFEST_INVALID",
                            "Firefox trust manifest disappeared during managed root rotation",
                        )
                    _validate_manifest_identity(
                        rebound_manifest,
                        firefox=firefox,
                        firefox_sha256=firefox_sha256,
                        profile=profile,
                    )
                    if rebound_manifest["root_fingerprint_sha256"] != material.root_fingerprint_sha256:
                        _raise(
                            "WINDOWS_FIREFOX_TRUST_MANIFEST_INVALID",
                            "Firefox trust manifest did not bind the rotated FactLane root",
                        )
                after = _snapshot_unrelated(profile)
                if after != before:
                    _raise(
                        "WINDOWS_FIREFOX_PROFILE_MUTATION_UNEXPECTED",
                        "Firefox NSS trust provisioning changed unrelated profile state",
                    )
                receipt = WindowsFirefoxTrustReceipt(
                    status=status,
                    firefox_executable=firefox,
                    firefox_sha256=firefox_sha256,
                    nss_dll_path=nss_path,
                    profile_dir=profile,
                    root_fingerprint_sha256=material.root_fingerprint_sha256,
                    ssl_trust_flags=ssl_flags,
                    db_initialized=not db_preexisting,
                )
                completed = True
                return receipt
            finally:
                if fresh_db_owned and not completed:
                    _cleanup_fresh_db_files(guard, remove_manifest=manifest is None)
    finally:
        nss_pin.close()
        firefox_pin.close()
