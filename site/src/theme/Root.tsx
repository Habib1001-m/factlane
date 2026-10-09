import type {ReactNode} from 'react';
import React from 'react';

function useMobileNavKeyboardGuard() {
  React.useEffect(() => {
    const restoreToggleFocus = () => {
      window.requestAnimationFrame(() => {
        document.querySelector<HTMLButtonElement>('.navbar__toggle')?.focus();
      });
    };

    const visibleSidebarFocusables = (sidebar: HTMLElement) => {
      const sidebarRect = sidebar.getBoundingClientRect();

      return Array.from(
        sidebar.querySelectorAll<HTMLElement>(
          'a[href], button:not([disabled]), [tabindex]:not([tabindex="-1"])',
        ),
      ).filter((element) => {
        const style = window.getComputedStyle(element);
        const rect = element.getBoundingClientRect();
        const visibleWidth =
          Math.min(rect.right, sidebarRect.right) - Math.max(rect.left, sidebarRect.left);
        const visibleHeight =
          Math.min(rect.bottom, sidebarRect.bottom) - Math.max(rect.top, sidebarRect.top);

        return (
          style.display !== 'none' &&
          style.visibility !== 'hidden' &&
          visibleWidth >= Math.min(rect.width, 24) &&
          visibleHeight >= Math.min(rect.height, 24)
        );
      });
    };

    const onKeyDown = (event: KeyboardEvent) => {
      const sidebar = document.querySelector<HTMLElement>(
        '.navbar-sidebar--show .navbar-sidebar',
      );
      if (!sidebar) {
        return;
      }

      if (event.key === 'Escape') {
        event.preventDefault();
        sidebar.querySelector<HTMLButtonElement>('.navbar-sidebar__close')?.click();
        restoreToggleFocus();
        return;
      }

      if (event.key !== 'Tab') {
        return;
      }

      const focusables = visibleSidebarFocusables(sidebar);
      if (focusables.length === 0) {
        return;
      }

      const active = document.activeElement as HTMLElement | null;
      const first = focusables[0];
      const last = focusables[focusables.length - 1];

      if (!active || !sidebar.contains(active)) {
        event.preventDefault();
        (event.shiftKey ? last : first).focus();
        return;
      }

      if (!event.shiftKey && active === last) {
        event.preventDefault();
        first.focus();
      } else if (event.shiftKey && active === first) {
        event.preventDefault();
        last.focus();
      }
    };

    const onClick = (event: MouseEvent) => {
      const target = event.target;
      if (target instanceof Element && target.closest('.navbar-sidebar__close')) {
        restoreToggleFocus();
      }
    };

    document.addEventListener('keydown', onKeyDown, true);
    document.addEventListener('click', onClick);

    return () => {
      document.removeEventListener('keydown', onKeyDown, true);
      document.removeEventListener('click', onClick);
    };
  }, []);
}

function useSsrRouteBoundaryRestore() {
  React.useEffect(() => {
    const routedWindow = window as typeof window & {
      __factlaneSsrPathRestore?: string;
    };
    const restore = routedWindow.__factlaneSsrPathRestore;
    if (!restore) {
      return;
    }

    window.history.replaceState(window.history.state, '', restore);
    delete routedWindow.__factlaneSsrPathRestore;
  }, []);
}

export default function Root({children}: {children: ReactNode}): ReactNode {
  useMobileNavKeyboardGuard();
  useSsrRouteBoundaryRestore();
  return <>{children}</>;
}
