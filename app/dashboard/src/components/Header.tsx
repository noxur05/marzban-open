import {
  Box,
  chakra,
  HStack,
  IconButton,
  Menu,
  MenuButton,
  MenuItem,
  MenuDivider,
  MenuGroup,
  MenuList,
  Text,
  useColorMode,
  useDisclosure,
} from "@chakra-ui/react";
import {
  ArrowLeftOnRectangleIcon,
  Bars3Icon,
  ChartPieIcon,
  Cog6ToothIcon,
  DocumentMinusIcon,
  LinkIcon,
  MegaphoneIcon,
  MoonIcon,
  PresentationChartLineIcon,
  SquaresPlusIcon,
  SunIcon,
} from "@heroicons/react/24/outline";
import { useDashboard } from "contexts/DashboardContext";
import { FC, ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { Link, useNavigate } from "react-router-dom";
import { updateThemeColor } from "utils/themeColor";
import { Language } from "./Language";
import { AnnouncementsModal } from "./AnnouncementsModal";
import { ForeignHostsModal } from "./ForeignHostsModal";
import useGetUser from "hooks/useGetUser";
import { GlobeAltIcon, UserGroupIcon } from "@heroicons/react/24/outline";

type HeaderProps = {
  actions?: ReactNode;
};
const iconProps = {
  baseStyle: {
    w: 4,
    h: 4,
  },
};

const DarkIcon = chakra(MoonIcon, iconProps);
const LightIcon = chakra(SunIcon, iconProps);
const CoreSettingsIcon = chakra(Cog6ToothIcon, iconProps);
const SettingsIcon = chakra(Bars3Icon, iconProps);
const LogoutIcon = chakra(ArrowLeftOnRectangleIcon, iconProps);
const HostsIcon = chakra(LinkIcon, iconProps);
const NodesIcon = chakra(SquaresPlusIcon, iconProps);
const NodesUsageIcon = chakra(ChartPieIcon, iconProps);
const AdminsUsageIcon = chakra(PresentationChartLineIcon, iconProps);
const ResetUsageIcon = chakra(DocumentMinusIcon, iconProps);
const AnnounceIcon = chakra(MegaphoneIcon, iconProps);
const AdminsIcon = chakra(UserGroupIcon, iconProps);
const ForeignHostsIcon = chakra(GlobeAltIcon, iconProps);

export const Header: FC<HeaderProps> = ({ actions }) => {
  const { userData, getUserIsSuccess, getUserIsPending } = useGetUser();

  const isSudo = () => {
    if (!getUserIsPending && getUserIsSuccess) {
      return userData.is_sudo;
    }
    return false;
  };

  const { onEditingHosts, onResetAllUsage, onEditingNodes, onShowingNodesUsage } =
    useDashboard();
  const { t } = useTranslation();
  const { colorMode, toggleColorMode } = useColorMode();
  const navigate = useNavigate();
  const announce = useDisclosure();
  const foreignHosts = useDisclosure();

  return (
    <>
    <HStack
      gap={2}
      justifyContent="space-between"
      __css={{
        "& .menuList": {
          direction: "ltr",
        },
      }}
      position="relative"
    >
      <Text as="h1" fontWeight="semibold" fontSize="2xl">
        {t("users")}
      </Text>
      <Box overflow="auto" css={{ direction: "rtl" }}>
        <HStack alignItems="center">
          <Menu>
            <MenuButton
              as={IconButton}
              size="sm"
              variant="outline"
              icon={
                <>
                  <SettingsIcon />
                </>
              }
              position="relative"
            ></MenuButton>
            <MenuList minW="220px" zIndex={99999} className="menuList">
              {isSudo() && (
                <>
                  <MenuGroup title={t("header.group.server")} fontSize="xs" color="gray.500">
                    <MenuItem fontSize="sm" icon={<HostsIcon />} onClick={onEditingHosts.bind(null, true)}>
                      {t("header.hostSettings")}
                    </MenuItem>
                    <MenuItem fontSize="sm" icon={<NodesIcon />} onClick={onEditingNodes.bind(null, true)}>
                      {t("header.nodeSettings")}
                    </MenuItem>
                    <MenuItem fontSize="sm" icon={<NodesUsageIcon />} onClick={onShowingNodesUsage.bind(null, true)}>
                      {t("header.nodesUsage")}
                    </MenuItem>
                    <MenuItem fontSize="sm" icon={<ForeignHostsIcon />} onClick={foreignHosts.onOpen}>
                      {t("foreignHosts.title")}
                    </MenuItem>
                  </MenuGroup>
                  <MenuDivider />
                  <MenuGroup title={t("header.group.administration")} fontSize="xs" color="gray.500">
                    <MenuItem fontSize="sm" icon={<AdminsIcon />} onClick={() => navigate("/admins/")}>
                      {t("admins.title")}
                    </MenuItem>
                    <MenuItem fontSize="sm" icon={<ResetUsageIcon />} onClick={onResetAllUsage.bind(null, true)}>
                      {t("resetAllUsage")}
                    </MenuItem>
                  </MenuGroup>
                  <MenuDivider />
                </>
              )}
              {/* Visible to all admins — backend scopes non-sudo admins to
                  their own data. */}
              <MenuItem fontSize="sm" icon={<AnnounceIcon />} onClick={announce.onOpen}>
                {t("announcements.title")}
              </MenuItem>
              <MenuItem fontSize="sm" icon={<AdminsUsageIcon />} onClick={() => navigate("/statistics/")}>
                {t("statistics.title")}
              </MenuItem>
              <MenuDivider />
              <Link to="/login">
                <MenuItem fontSize="sm" icon={<LogoutIcon />}>
                  {t("header.logout")}
                </MenuItem>
              </Link>
            </MenuList>
          </Menu>

          {isSudo() && (
            <IconButton
              size="sm"
              variant="outline"
              aria-label="core settings"
              onClick={() => {
                useDashboard.setState({ isEditingCore: true });
              }}
            >
              <CoreSettingsIcon />
            </IconButton>
          )}

          <Language />

          <IconButton
            size="sm"
            variant="outline"
            aria-label="switch theme"
            onClick={() => {
              updateThemeColor(colorMode == "dark" ? "light" : "dark");
              toggleColorMode();
            }}
          >
            {colorMode === "light" ? <DarkIcon /> : <LightIcon />}
          </IconButton>
        </HStack>
      </Box>
    </HStack>
    <AnnouncementsModal isOpen={announce.isOpen} onClose={announce.onClose} />
    <ForeignHostsModal isOpen={foreignHosts.isOpen} onClose={foreignHosts.onClose} />
    </>
  );
};
