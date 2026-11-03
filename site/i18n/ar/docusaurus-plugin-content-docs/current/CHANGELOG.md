# سجل تغييرات FactLane

يسجل هذا الـchangelog التغييرات الظاهرة للمستخدم ومتطلبات الترقية لكل إصدار. ولإجراءات التثبيت
والترحيل والتراجع وهوية الإصدار الدقيقة، استخدم
[عمليات الإصدار](RELEASE_OPERATIONS.md).

## 0.1.5 release candidate — بانتظار النشر

FactLane 0.1.5 هو حاليًا release candidate. يعلن المصدر package version `0.1.5` وPublic Contract
Revision 3 وstorage contract v3، لكن هذه التصريحات في المصدر لا تنشئ إصدارًا رسميًا. ويظل
annotated tag النهائي وrelease commit/tree وdigests الخاصة بالـwheel وsource distribution وحالة
PyPI وأي نشر للموقع بانتظار خطوات نشر منفصلة ومصرح بها.

### أبرز التغييرات

- يضيف Owner control plane محكومًا لمراجعة الذاكرة الدائمة والعمل عليها من دون توسيع سطح MCP
  العام خارج الأدوات الخمس الحالية.
- يضيف disposition مباشرًا من Owner عبر `REVOKE` و`REJECT`، وحماية compare-and-swap لمجموعة
  التناقض، وسجلًا دائمًا لأحداث/عمليات lifecycle، وreadmission محكومًا.
- يضيف انتقالًا صريحًا من storage-v2 إلى storage-v3 مع backup متحقق منه قبل v3 ومسارًا محددًا
  للتراجع عن البيانات. ولا تُرقّى قواعد storage-v2 الموجودة بصمت أثناء startup العادي.
- يضيف إعادة فحص يدوية لحداثة `on_change` بنتائج مرتبطة بالدليل. ولا يضيف FactLane watcher
  تلقائيًا للمصدر.
- يرفق Owner CLI محليًا وWebUI عبر loopback مع إبقاء نقل MCP عبر `stdio` يُشغَّل بالأمر فقط.

### ما أُضيف

- `OwnerControlService` للفحص المحدود للمخزون والتفاصيل، ومراجعة التعارضات، وحالة الصحة
  والاحتفاظ، وإجراءات lifecycle، وإعادة الفحص اليدوية للحداثة، وreadmission.
- `factlane-owner`، وهو Owner CLI موثوق ومرفق. تربط launcher arguments نطاق Owner وهوية الـhost؛
  ولا يمكن لحمولة mutation استبدال تلك السلطة المربوطة.
- `factlane-owner-webui`، وهو Owner WebUI محلي مرفق ومربوط بالضبط إلى `127.0.0.1` وإلى نطاق
  واحد غير قابل للتغيير. يصدر startup bootstrap token قصير العمر للاستخدام مرة واحدة ولا يُتبادل
  إلا عبر header `X-FactLane-Bootstrap`. وتتطلب mutations الموثقة session الناتجة وCSRF token.
  والـWebUI سطح عرض للـOwner، لا HTTP MCP transport ولا remote-admin service.
- `factlane-migrate-storage-v3`، وهو مشغّل الترحيل/الاستعادة الموثوق والصريح للانتقال من v2 إلى v3.
- readmission محكوم لذاكرة منطقية مؤهلة في `REVOKED` بعد تحقق جديد وفحوص CAS حالية. ينشئ
  readmission successor محكومًا؛ وليس backup restore ولا يستطيع إعادة إنشاء محتوى أزاله
  sensitive-memory Recovery.
- سجلات دائمة لأحداث lifecycle وعمليات lifecycle لقرارات حوكمة storage-v3.

### ما تغيّر

- يتقدم Public Contract Revision من 2 إلى 3. وتبقى مجموعة أدوات MCP العامة بالضبط:
  `memory_search` و`memory_get` و`memory_store` و`memory_update` و`memory_status`.
- تدعم `memory_update` الآن `REVOKE` و`REJECT` بصلاحية Owner مباشرة إلى جانب `REVERIFY`
  و`REPLACE`. تتطلب dispositions الخاصة بالـOwner revision الحالي الدقيق وملاحظة مجموعة التناقض
  وحماية idempotency وreason code محدودًا.
- يتقدم storage contract من v2 إلى v3. ويضيف storage v3 حواجز writer محكومة وGroup CAS لمجموعة
  التناقض وlifecycle history وحماية idempotency عامة عبر adapter writes العادية وعمليات Owner lifecycle.
- `REVOKED` و`REJECTED` حالتا lifecycle منطقيتان نهائيتان. ولا تمثلان محوًا ماديًا لمحتوى قاعدة
  البيانات.
- يمكن الآن إعادة فحص حداثة `on_change` عبر عملية Owner صريحة. قد تجدد ملاحظة unchanged الأهلية؛
  بينما يفشل الدليل changed أو unavailable أو indeterminate أو stale أو mismatched أو غير المقبول
  مغلقًا للمراجعة.

### ترقية مطلوبة: من storage v2 إلى v3

ترقية قاعدة v0.1.4 دائمة هي **ترحيل بيانات/schema**، لا تحديث package فقط. يفحص startup العادي
في 0.1.5 قاعدة موجودة ويعيد `MIGRATION_REQUIRED` عند storage v2. لا تربط runtime 0.1.5 بهذه
القاعدة وتتوقع منه ترقية schema تلقائيًا.

قبل الترحيل:

1. أوقف FactLane أو ضعه في quiescence ليحصل مشغّل الترحيل على maintenance access حصري.
2. اختر مسار backup جديدًا داخل مجلد private موجود ويملكه مشغّل الترحيل.
   يجب ألا يمنح parent الخاص بالـbackup صلاحيات للمجموعة أو للآخرين (`mode & 077 == 0`).
3. احتفظ بهوية runtime/source الحالية لـv0.1.4 حتى يكتمل الترحيل والتحقق بعده.

نفّذ الترحيل فقط على maintenance host مدعوم من POSIX:

```bash
uv run factlane-migrate-storage-v3 migrate \
  --db /absolute/path/to/factlane.sqlite3 \
  --backup /absolute/path/to/backups/factlane-pre-v3.sqlite3
```

يتحقق المشغّل من storage v2 الدقيق وlineage، وينشئ ويتحقق من backup وverification manifest
المملوكين للمشغّل بدلالات private/no-replace، ثم يطبق governance schema v3 transactionally ويرفع
contract version في النهاية. احتفظ بمسار backup وSHA-256 المبلغ عنهما كدليل rollback.

إذا كان يجب التخلي عن حالة v3 قبل قبولها كمرجعية دائمة، فأوقف FactLane واستعد backup الدقيق
والمتحقق منه قبل v3 عبر مشغّل الترحيل:

```bash
uv run factlane-migrate-storage-v3 restore \
  --db /absolute/path/to/factlane.sqlite3 \
  --backup /absolute/path/to/backups/factlane-pre-v3.sqlite3
```

تثبيت أو إعادة تشغيل v0.1.4 مباشرة على قاعدة storage-v3 ليس downgrade مدعومًا للبيانات. راجع
[عمليات الإصدار](RELEASE_OPERATIONS.md) لعقد الترحيل والتراجع الكامل.

### الأمان

- ترتبط قرارات lifecycle الخاصة بالـOwner المباشر بسلطة launcher موثوقة وحالة revision الدقيقة
  وGroup CAS لمجموعة التناقض وidempotency وreason codes محدودة. يظل delegated agent العادي
  Candidate-only ولا يستطيع ترقية نفسه بتغيير حقول الطلب.
- يضيف storage v3 writer fencing محليًا لكل connection وفحوص idempotency عامة حتى لا يستطيع
  writer قديم/غير موثوق أو إعادة استخدام المفتاح عبور فئات mutation المحكومة بصمت.
- الـOwner WebUI يعمل عبر loopback فقط ويستخدم bootstrap exchange لمرة واحدة وcookie session
  من نوع HttpOnly; SameSite=Strict وفحوص origin/host وحماية CSRF للـmutations.
- تلقت تبعيات Python/runtime وsite-toolchain تحديثات أمان خلال دورة 0.1.5. وهذا ليس ادعاء
  zero-advisory: لا تزال سلسلة build الحالية في Docusaurus 3.10.2 تحمل advisories متبقية
  upstream-blocked في build/dev/preview dependencies. هذه الحزم جزء من site toolchain المؤهل
  بصورة منفصلة؛ وشكل الموقع production الذي تمت مراجعته static output وليس Node/Docusaurus
  application server. يظل خطر build-time وsupply-chain المتبقي قرار إصدار ولا يُمثّل على أنه
  عولج.
- يظل sensitive-memory Recovery مسار صيانة منفصلًا ومصرحًا به للمشغّل. ولا تستبدل
  `REVOKE`/`REJECT` المنطقية وreadmission ضوابط الاستعادة المادية للذاكرة الحساسة.

### التوافق

- يظل متطلب Python هو `>=3.11` ويظل الحد الأدنى لـSQLite المرتبط هو `>=3.42.0`.
- يظل نقل MCP عبر `stdio` يُشغَّل بالأمر فقط، مع خمس أدوات عامة بالضبط.
- يستمر دعم native Windows x64/AMD64 **لوقت التشغيل العادي** الذي أُنشئ لـv0.1.4. ولا يوسع ذلك
  حدود الصيانة: يظل sensitive-memory Recovery وstorage-v2 إلى v3 migration/restore POSIX-only.
  ولا يوجد ادعاء بدعم Windows ARM64.
- تظل قواعد embedding-space compatibility الموجودة في v0.1.4 منفصلة عن schema migration الجديد
  في storage-v3. نجاح تثبيت package وحده لا يثبت توافق البيانات الدائمة.
- تظل Python package والموقع العام سطحَي إصدار منفصلين. لا تتضمن wheel/source distribution
  المؤهلة الموقع، ولا يؤدي تأهيل الموقع أو نشره إلى نشر Python package.

### الحدود المعروفة

- لا يراقب `on_change` المصادر تلقائيًا. يجب أن يقدّم Owner موثوق ملاحظة حديثة ومحدودة لإعادة
  الفحص اليدوية.
- الـOwner WebUI سطح مشغّل محلي عبر loopback، وليس remote administration مدعومًا ولا HTTP MCP server.
- `REVOKED`/`REJECTED` حالتا نهاية منطقيتان؛ ولا تدعيان الحذف المادي.
- يقتصر readmission على ذاكرة `REVOKED` المؤهلة ولا يستطيع استعادة محتوى أزيل ماديًا بواسطة
  sensitive-memory Recovery.
- storage-v2 إلى v3 migration/restore وsensitive-memory Recovery عمليات صيانة POSIX-only.
- تظل semantic relevance وجودة اللغة وسلوك filesystem خارج الملفات المؤهلة وحجم deployment
  خاصة بعبء العمل والبيئة بدل أن تكون ضمانات عامة.

### هوية الإصدار بانتظار النشر

لا تُملأ الحقول التالية إلا من عبور الإصدار المصرح به بصورة منفصلة. لا تستنتجها من فرع `main`
المتحرك أو local RC builds أو workflow configuration أو candidate artifacts.

| العنصر | 0.1.5 release candidate |
| --- | --- |
| Package version | `0.1.5` |
| Public Contract Revision | `3` |
| Storage contract | `3` |
| Public MCP transport | `stdio` |
| Public MCP tools | `memory_search`, `memory_get`, `memory_store`, `memory_update`, `memory_status` |
| Annotated tag | بانتظار النشر |
| Tag object | بانتظار النشر |
| Release commit | بانتظار النشر |
| Release tree | بانتظار النشر |
| Wheel name / SHA-256 | بانتظار النشر |
| Source distribution name / SHA-256 | بانتظار النشر |
| PyPI publication | بانتظار النشر |
| Site Production deployment | سطح publication منفصل؛ لا تتضمنه هذه package release ضمنيًا |

## 0.1.4

حافظ FactLane v0.1.4 على Public Contract Revision 2 وعلى سطح MCP نفسه ذي الأدوات الخمس، وأضاف
مسار embedding-space compatibility المؤهل من v0.1.3، وتأهيل native Windows x64 لوقت التشغيل
العادي، وإرشادات bootstrap للـSkill المحمولة، وضوابط package/release أقوى، وسطح publication ثنائي
اللغة. وتظل هوية الإصدار التاريخية الدقيقة وملاحظات الترحيل في
[عمليات الإصدار](RELEASE_OPERATIONS.md).

## 0.1.3

كان FactLane v0.1.3 أول إصدار إنتاج رسمي. وتظل هويته التاريخية الدقيقة وحدود دعمه في
[عمليات الإصدار](RELEASE_OPERATIONS.md).
