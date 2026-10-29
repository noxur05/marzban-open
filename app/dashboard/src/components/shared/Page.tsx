import { Box, Button, Flex, HStack, Icon, Progress, Text } from "@chakra-ui/react";
import { ArrowLeftIcon } from "@heroicons/react/24/outline";
import { FC, ReactNode } from "react";

/**
 * A full page (Statistics, Admins): phone gutters, a capped width on desktop.
 * A plain flex column, not a VStack: a Stack resets its children's side
 * margins, which would undo StickyBar's full-bleed negative margin.
 */
export const Page: FC<{ children: ReactNode }> = ({ children }) => (
  <Flex
    direction="column"
    minH="100vh"
    px={{ base: 4, md: 6 }}
    pb={8}
    maxW="1200px"
    mx="auto"
    w="full"
  >
    {children}
  </Flex>
);

/** The page title with a way back to the users list. */
export const PageHeader: FC<{
  title: ReactNode;
  backLabel: string;
  onBack: () => void;
}> = ({ title, backLabel, onBack }) => (
  <HStack justify="space-between" pt={{ base: 3, md: 5 }} pb={2}>
    <Text as="h1" fontWeight="semibold" fontSize={{ base: "xl", md: "2xl" }}>
      {title}
    </Text>
    <Button
      size="sm"
      variant="ghost"
      minH="44px"
      leftIcon={<Icon as={ArrowLeftIcon} boxSize="16px" />}
      onClick={onBack}
    >
      {backLabel}
    </Button>
  </HStack>
);

/**
 * Filters and views that stay in reach while the page scrolls, with a thin
 * loading stripe under them while anything on the page refetches.
 */
export const StickyBar: FC<{ busy?: boolean; children: ReactNode }> = ({
  busy,
  children,
}) => (
  <Box
    position="sticky"
    top={0}
    zIndex="sticky"
    bg="var(--chakra-colors-chakra-body-bg)"
    mx={{ base: -4, md: -6 }}
    px={{ base: 4, md: 6 }}
    pt={2}
  >
    {children}
    {/* Clipped: the indeterminate stripe slides past the edges. */}
    <Box h="2px" overflow="hidden">
      {busy && (
        <Progress
          size="xs"
          h="2px"
          isIndeterminate
          colorScheme="primary"
          bg="transparent"
        />
      )}
    </Box>
  </Box>
);
