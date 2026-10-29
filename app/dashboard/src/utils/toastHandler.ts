import { CreateToastFnReturn } from "@chakra-ui/react";
import { UseFormReturn } from "react-hook-form";

export const generateErrorMessage = (
  e: any,
  toast: CreateToastFnReturn,
  form?: UseFormReturn<any>
) => {
  if (e.response && e.response._data) {
    if (typeof e.response._data.detail === "string")
      return toast({
        title: e.response._data.detail,
        status: "error",
        isClosable: true,
        position: "top",
        duration: 3000,
      });
    if (typeof e.response._data.detail === "object")
      if (form) {
        Object.keys(e.response._data.detail).forEach((errorKey) =>
          form.setError(errorKey, {
            message: e.response._data.detail[errorKey],
          })
        );
        return;
      }
  }
  return toast({
    title: "Something went wrong!",
    status: "error",
    isClosable: true,
    position: "top",
    duration: 3000,
  });
};

/**
 * A displayable string for an API error, whatever shape `detail` arrived in.
 *
 * Never hand `detail` straight to a Chakra toast title. It is only a string for
 * an `HTTPException`; this panel's own 422 handler (app/__init__.py) returns an
 * OBJECT keyed by field name, and a stock FastAPI validation error returns an
 * ARRAY. React throws "Objects are not valid as a React child" on either, which
 * unmounts the whole modal tree — a validation mistake in one input would take
 * the dashboard down to a white screen.
 */
export const errorDetail = (e: any, fallback: string): string => {
  const detail = e?.response?._data?.detail;

  if (typeof detail === "string" && detail.trim()) return detail;

  if (Array.isArray(detail)) {
    const msgs = detail
      .map((d: any) => (typeof d === "string" ? d : d?.msg))
      .filter((m: any) => typeof m === "string" && m.trim());
    if (msgs.length) return msgs.join("; ");
  }

  if (detail && typeof detail === "object") {
    const msgs = Object.entries(detail)
      .map(([field, msg]) => (typeof msg === "string" ? `${field}: ${msg}` : null))
      .filter(Boolean) as string[];
    if (msgs.length) return msgs.join("; ");
  }

  return fallback;
};

export const generateSuccessMessage = (
  message: string,
  toast: CreateToastFnReturn
) => {
  return toast({
    title: message,
    status: "success",
    isClosable: true,
    position: "top",
    duration: 3000,
  });
};
