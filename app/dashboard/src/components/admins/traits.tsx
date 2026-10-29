import { Badge, Flex } from "@chakra-ui/react";
import { AdminListItem } from "hooks/useAdmins";
import { FC } from "react";
import { useTranslation } from "react-i18next";

/**
 * How an admin differs from a standard reseller (non-sudo, device-limited
 * users only, no own announcement, no Outline, the usual domain). A standard
 * reseller has no traits, so the list shows chips only where something is
 * different.
 */
export type TraitKey = "env" | "sudo" | "limited" | "unrestricted" | "announces" | "outline" | "domain";
export type Trait = { key: TraitKey; label: string };

const COLOR: Record<TraitKey, string> = {
  env: "purple",
  sudo: "purple",
  // Only the create form names the standard; the list leaves it unsaid.
  limited: "gray",
  unrestricted: "orange",
  announces: "blue",
  outline: "teal",
  domain: "gray",
};

export const host = (url: string) => url.replace(/^https?:\/\//, "");

/** Every domain admins use, most used first ("" = the panel default). */
export const domainsInUse = (admins: AdminListItem[]): string[] => {
  const counts = new Map<string, number>();
  for (const a of admins) {
    if (typeof a.id !== "number") continue;
    const d = a.subscription_url ?? "";
    counts.set(d, (counts.get(d) ?? 0) + 1);
  }
  return [...counts.keys()].sort((x, y) => counts.get(y)! - counts.get(x)! || x.localeCompare(y));
};

/** The domain most admins use: the one every other is compared with. */
export const commonDomain = (admins: AdminListItem[]): string => domainsInUse(admins)[0] ?? "";

export type FlagKey = "happ_only" | "can_announce" | "can_enable_outline";

/** The switches a sudo sets on an admin, in the chips' order. */
export const FLAGS: { key: FlagKey; label: string; help: string }[] = [
  { key: "happ_only", label: "announcements.happOnly", help: "admins.flags.happOnlyHelp" },
  { key: "can_announce", label: "announcements.canAnnounce", help: "admins.flags.canAnnounceHelp" },
  { key: "can_enable_outline", label: "announcements.canEnableOutline", help: "admins.flags.canEnableOutlineHelp" },
];

/** All of a new admin's access as chips, the standard named too: the list
 * leaves it unsaid, the create form confirms it. */
export const useAccessTraits = () => {
  const { t } = useTranslation();
  return (a: Partial<Record<FlagKey, boolean>>, domain: string): Trait[] => [
    a.happ_only
      ? { key: "limited", label: t("admins.trait.limited") }
      : { key: "unrestricted", label: t("admins.trait.unrestricted") },
    ...(a.can_announce ? [{ key: "announces", label: t("admins.trait.announces") } as Trait] : []),
    ...(a.can_enable_outline ? [{ key: "outline", label: t("admins.trait.outline") } as Trait] : []),
    { key: "domain", label: domain },
  ];
};

export const useTraits = () => {
  const { t } = useTranslation();
  return (a: AdminListItem, domain: string): Trait[] => {
    // The env sudoer has no row: nothing about it can be edited here.
    if (typeof a.id !== "number") return [{ key: "env", label: t("admins.trait.env") }];
    const out: Trait[] = [];
    // A sudo bypasses every restriction, so the flags say nothing about it.
    if (a.is_sudo) out.push({ key: "sudo", label: t("admins.trait.sudo") });
    else {
      if (!a.happ_only) out.push({ key: "unrestricted", label: t("admins.trait.unrestricted") });
      if (a.can_announce) out.push({ key: "announces", label: t("admins.trait.announces") });
      if (a.can_enable_outline) out.push({ key: "outline", label: t("admins.trait.outline") });
    }
    const own = a.subscription_url ?? "";
    if (own !== domain)
      out.push({ key: "domain", label: own ? host(own) : t("admins.trait.defaultDomain") });
    return out;
  };
};

/** The chips, wrapping onto more lines rather than being cut off. */
export const TraitBadges: FC<{ traits: Trait[] }> = ({ traits }) => (
  <Flex wrap="wrap" gap={1}>
    {traits.map((tr) => (
      <Badge
        key={tr.key}
        colorScheme={COLOR[tr.key]}
        variant={tr.key === "domain" ? "outline" : "subtle"}
        textTransform="none"
        fontWeight="medium"
        maxW="full"
        overflow="hidden"
        textOverflow="ellipsis"
      >
        {tr.label}
      </Badge>
    ))}
  </Flex>
);
