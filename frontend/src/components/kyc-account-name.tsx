import type { ReactNode } from "react";
import { Crown } from "lucide-react";

import { cn } from "@/lib/utils";

type KycAccountNameProps = {
  children: ReactNode;
  kyc?: boolean;
  blurred?: boolean;
  className?: string;
};

export function KycAccountName({
  children,
  kyc = false,
  blurred = false,
  className,
}: KycAccountNameProps) {
  if (!kyc) {
    return (
      <span className={cn(blurred ? "privacy-blur" : undefined, className)}>
        {children}
      </span>
    );
  }

  return (
    <span className={cn("inline-flex min-w-0 max-w-full items-center gap-1.5 align-bottom", className)}>
      <span
        aria-hidden="true"
        className="inline-flex h-4 w-4 shrink-0 items-center justify-center rounded-full border border-amber-300/80 bg-[radial-gradient(circle_at_35%_25%,#fff7ad_0%,#f6c945_42%,#b7791f_100%)] text-amber-950 shadow-[0_0_18px_rgba(245,158,11,0.42)] dark:border-amber-200/70 dark:shadow-[0_0_20px_rgba(251,191,36,0.36)]"
      >
        <Crown className="h-2.5 w-2.5" strokeWidth={2.4} />
      </span>
      <span
        className={cn(
          "min-w-0 truncate bg-[linear-gradient(110deg,#8a4b00_0%,#d9951e_34%,#fff2a8_50%,#c47a10_68%,#7a4300_100%)] bg-clip-text font-semibold text-transparent drop-shadow-[0_0_8px_rgba(245,158,11,0.38)] dark:bg-[linear-gradient(110deg,#ffe58a_0%,#f6c945_38%,#fff8c9_50%,#d99a24_72%,#facc15_100%)]",
          blurred ? "privacy-blur" : undefined,
        )}
      >
        {children}
      </span>
    </span>
  );
}
