import { Alert, AlertIcon, Button, HStack, Icon, Text } from "@chakra-ui/react";
import { ArrowPathIcon } from "@heroicons/react/24/outline";
import { FC, ReactNode } from "react";
import { useTranslation } from "react-i18next";

/** A failed load, with Retry beside the message. */
export const LoadError: FC<{ message: ReactNode; onRetry: () => void }> = ({
  message,
  onRetry,
}) => {
  const { t } = useTranslation();
  return (
    <Alert status="error" borderRadius="md" fontSize="sm">
      <AlertIcon />
      <HStack justify="space-between" w="full" flexWrap="wrap" gap={2}>
        <Text>{message}</Text>
        <Button
          size="xs"
          variant="outline"
          leftIcon={<Icon as={ArrowPathIcon} boxSize="14px" />}
          onClick={onRetry}
        >
          {t("adminsUsage.retry")}
        </Button>
      </HStack>
    </Alert>
  );
};
