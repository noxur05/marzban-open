import {
  FormControl,
  FormControlProps,
  FormErrorMessage,
  FormHelperText,
  FormLabel,
  HStack,
} from "@chakra-ui/react";
import { FC, PropsWithChildren, ReactNode } from "react";

export type FormFieldProps = PropsWithChildren<
  {
    label?: ReactNode;
    /** Rendered on the right of the label row (a counter, a link, a switch). */
    labelAddon?: ReactNode;
    helper?: ReactNode;
    /** A string error shows `FormErrorMessage` and marks the control invalid.
     * Pass `form.formState.errors.x?.message` straight in. */
    error?: string | null | false;
  } & Omit<FormControlProps, "label">
>;

/**
 * Label + control + helper + error, the way UserDialog / NodesModal lay out
 * fields. The fork's newer dialogs used bare inputs with placeholder-as-label
 * and toasts for validation; this puts them on the same footing.
 */
export const FormField: FC<FormFieldProps> = ({
  label,
  labelAddon,
  helper,
  error,
  children,
  ...control
}) => (
  <FormControl isInvalid={!!error} {...control}>
    {(label || labelAddon) && (
      <HStack justifyContent="space-between" alignItems="center" mb={1}>
        {label ? <FormLabel mb={0}>{label}</FormLabel> : <span />}
        {labelAddon}
      </HStack>
    )}
    {children}
    {error ? (
      <FormErrorMessage>{error}</FormErrorMessage>
    ) : helper ? (
      <FormHelperText color="gray.600" _dark={{ color: "gray.400" }}>
        {helper}
      </FormHelperText>
    ) : null}
  </FormControl>
);
