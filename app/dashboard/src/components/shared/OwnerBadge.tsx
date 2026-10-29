import { Badge, BadgeProps } from "@chakra-ui/react";
import { useDashboard } from "contexts/DashboardContext";
import { FC } from "react";
import { useTranslation } from "react-i18next";
import { User } from "types/User";

export type OwnerBadgeProps = {
  user: User;
} & Omit<BadgeProps, "onClick" | "children">;

/**
 * The owning admin's name on a user row, and the entry point for transferring
 * that user to a different admin.
 *
 * Lives here because the users table renders rows twice — an accordion layout
 * and a wide table layout — and the badge was duplicated across both, where
 * the two copies had already drifted apart in their styling props.
 *
 * The caller decides whether to render it at all: `UsersTable` only does so
 * for sudo, matching the backend, where `PUT /api/user/{u}/set-owner` is
 * sudo-gated regardless.
 *
 * `stopPropagation` matters because both parent rows have their own click
 * handler — the accordion row toggles itself open, the table row opens the
 * edit dialog — and neither is what clicking the owner should do.
 */
export const OwnerBadge: FC<OwnerBadgeProps> = ({ user, ...props }) => {
  const { t } = useTranslation();
  if (!user.admin?.username) return null;
  return (
    <Badge
      as="button"
      type="button"
      title={t("transfer.badgeTooltip")}
      cursor="pointer"
      _hover={{ opacity: 0.75 }}
      colorScheme={user.admin.is_sudo ? "purple" : "gray"}
      fontSize="2xs"
      textTransform="none"
      maxW="100%"
      overflow="hidden"
      whiteSpace="nowrap"
      textOverflow="ellipsis"
      onClick={(e) => {
        e.stopPropagation();
        useDashboard.setState({ transferUser: user });
      }}
      {...props}
    >
      {user.admin.username}
    </Badge>
  );
};
