import { useToast } from "@chakra-ui/react";
import { useCallback } from "react";
import { errorDetail } from "utils/toastHandler";

/**
 * One toast style for the whole panel — the literals every existing call
 * site retyped (`position: "top"`, `isClosable`, 3 s). `error()` accepts the
 * raw caught value and unwraps FastAPI's `detail` (string / object / array)
 * safely via `errorDetail`, so a validation error can never crash a render.
 */
export const useAppToast = () => {
  const toast = useToast();
  const success = useCallback(
    (title: string, description?: string): void => {
      toast({
        title,
        description,
        status: "success",
        isClosable: true,
        position: "top",
        duration: 3000,
      });
    },
    [toast]
  );
  const info = useCallback(
    (title: string, description?: string): void => {
      toast({
        title,
        description,
        status: "info",
        isClosable: true,
        position: "top",
        duration: 3000,
      });
    },
    [toast]
  );
  const error = useCallback(
    (e: unknown, fallback: string): void => {
      toast({
        title: errorDetail(e, fallback),
        status: "error",
        isClosable: true,
        position: "top",
        duration: 3000,
      });
    },
    [toast]
  );
  return { success, info, error };
};
