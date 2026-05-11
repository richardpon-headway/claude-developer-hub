import type { ButtonHTMLAttributes, ReactNode } from "react";

type Variant = "primary" | "secondary" | "danger";

interface Props extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant;
  children: ReactNode;
}

const styles: Record<Variant, string> = {
  primary:
    "bg-indigo-500 hover:bg-indigo-400 disabled:bg-indigo-500/40 text-white",
  secondary:
    "bg-zinc-800 hover:bg-zinc-700 disabled:bg-zinc-800/40 text-zinc-100 border border-zinc-700",
  danger:
    "bg-red-600 hover:bg-red-500 disabled:bg-red-600/40 text-white",
};

export function Button({ variant = "primary", className = "", children, ...rest }: Props) {
  return (
    <button
      {...rest}
      className={`inline-flex items-center justify-center rounded px-3 py-1.5 text-sm font-medium transition-colors disabled:cursor-not-allowed ${styles[variant]} ${className}`}
    >
      {children}
    </button>
  );
}
