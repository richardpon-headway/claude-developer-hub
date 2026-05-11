import { useEffect, type ReactNode } from "react";
import { createPortal } from "react-dom";

// Minimal modal: backdrop + centered panel + ESC closes. Hand-rolled rather
// than depending on Radix/shadcn for this slice. No focus trap yet — fine
// for the single-button single-modal flow CDH currently has; revisit when
// the UI grows multiple competing dialogs.

interface Props {
  open: boolean;
  onClose: () => void;
  title: string;
  children: ReactNode;
}

export function Dialog({ open, onClose, title, children }: Props) {
  useEffect(() => {
    if (!open) return;
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [open, onClose]);

  if (!open) return null;

  return createPortal(
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60"
      onClick={onClose}
      role="dialog"
      aria-modal="true"
      aria-label={title}
    >
      <div
        className="w-full max-w-2xl rounded-lg border border-zinc-800 bg-zinc-900 p-6 shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        <h2 className="text-lg font-semibold text-zinc-100">{title}</h2>
        <div className="mt-4">{children}</div>
      </div>
    </div>,
    document.body,
  );
}
