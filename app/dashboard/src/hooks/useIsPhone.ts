import { useBreakpointValue } from "@chakra-ui/react";

/**
 * Phone layout below md. Chakra answers the base value on the first render,
 * so a desktop briefly gets the phone layout, never the other way round.
 */
export const useIsPhone = () =>
  useBreakpointValue({ base: true, md: false }) ?? true;
