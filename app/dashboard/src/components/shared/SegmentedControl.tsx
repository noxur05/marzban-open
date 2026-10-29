import { Button, ButtonGroup, ButtonGroupProps } from "@chakra-ui/react";
import { ReactNode } from "react";

export type SegmentedOption<T extends string> = { value: T; label: ReactNode; isDisabled?: boolean };

/**
 * A row of attached toggle buttons with one active segment — presets,
 * metric switches, granularity. Chakra's `ButtonGroup isAttached` with the
 * panel's `primary` scheme, so every such control looks the same.
 */
export function SegmentedControl<T extends string>({
  value,
  onChange,
  options,
  size = "sm",
  ...group
}: {
  value: T;
  onChange: (v: T) => void;
  options: SegmentedOption<T>[];
  size?: "xs" | "sm" | "md";
} & Omit<ButtonGroupProps, "onChange" | "size">) {
  return (
    <ButtonGroup isAttached size={size} variant="outline" {...group}>
      {options.map((o) => {
        const active = o.value === value;
        return (
          <Button
            key={o.value}
            onClick={() => onChange(o.value)}
            isDisabled={o.isDisabled}
            aria-pressed={active}
            colorScheme={active ? "primary" : undefined}
            variant={active ? "solid" : "outline"}
            fontWeight={active ? "semibold" : "normal"}
          >
            {o.label}
          </Button>
        );
      })}
    </ButtonGroup>
  );
}
