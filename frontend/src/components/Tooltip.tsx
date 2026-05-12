import * as RadixTooltip from "@radix-ui/react-tooltip";
import type { ReactNode } from "react";

interface TooltipProps {
  text: ReactNode;
  children: ReactNode;
  side?: "top" | "right" | "bottom" | "left";
  disabled?: boolean;
}

export function Tooltip({ text, children, side = "top", disabled }: TooltipProps) {
  if (disabled || text == null || text === "") return <>{children}</>;

  return (
    <RadixTooltip.Root delayDuration={150}>
      <RadixTooltip.Trigger asChild>
        {/* The wrapping span keeps hover/focus events firing even when the
            child is a disabled <button> (which suppresses pointer events). */}
        <span className="inline-flex">{children}</span>
      </RadixTooltip.Trigger>
      <RadixTooltip.Portal>
        <RadixTooltip.Content
          side={side}
          sideOffset={4}
          collisionPadding={8}
          className="z-50 whitespace-nowrap rounded border border-zinc-700 bg-zinc-900 px-2 py-1 text-xs text-zinc-200 shadow-lg"
        >
          {text}
          <RadixTooltip.Arrow className="fill-zinc-700" />
        </RadixTooltip.Content>
      </RadixTooltip.Portal>
    </RadixTooltip.Root>
  );
}
