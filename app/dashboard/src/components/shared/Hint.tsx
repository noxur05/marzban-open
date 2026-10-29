import { Text, TextProps } from "@chakra-ui/react";
import { FC } from "react";

/**
 * Secondary text in the panel's muted colour pair (`gray.600` light /
 * `gray.400` dark — the pair StatusBadge and DeleteUserModal use, which
 * passes AA on both backgrounds). Replaces the `gray.500`-with-no-`_dark`
 * captions scattered through the fork's dialogs.
 */
export const Hint: FC<TextProps & { size?: "xs" | "sm" }> = ({
  size = "xs",
  ...props
}) => (
  <Text
    fontSize={size}
    color="gray.600"
    _dark={{ color: "gray.400" }}
    {...props}
  />
);
