import { Suspense, lazy, useCallback, useMemo } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";
import { toast } from "sonner";

import { ConfirmDialog } from "@/components/confirm-dialog";
import { AlertMessage } from "@/components/alert-message";
import { LoadingOverlay } from "@/components/layout/loading-overlay";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { useDialogState } from "@/hooks/use-dialog-state";
import { AccountDetail } from "@/features/accounts/components/account-detail";
import { AccountList } from "@/features/accounts/components/account-list";
import { AccountsSkeleton } from "@/features/accounts/components/accounts-skeleton";
import { ApiProviderDialog } from "@/features/accounts/components/api-provider-dialog";
import { ImportDialog } from "@/features/accounts/components/import-dialog";
import {
  useAccountResetCreditMap,
  useAccounts,
} from "@/features/accounts/hooks/use-accounts";
import { useOauth } from "@/features/accounts/hooks/use-oauth";
import { getSettings, updateSettings } from "@/features/settings/api";
import { buildSettingsUpdateRequest } from "@/features/settings/payload";
import type {
  DashboardSettings,
  SettingsUpdateRequest,
} from "@/features/settings/schemas";
import { buildDuplicateAccountIdSet } from "@/utils/account-identifiers";
import { getErrorMessageOrNull } from "@/utils/errors";

const OauthDialog = lazy(() =>
  import("@/features/accounts/components/oauth-dialog").then((m) => ({
    default: m.OauthDialog,
  })),
);

export function AccountsPage() {
  const queryClient = useQueryClient();
  const [searchParams, setSearchParams] = useSearchParams();
  const {
    accountsQuery,
    importMutation,
    createProviderMutation,
    updatePriorityMutation,
    updateAllFastServiceTierMutation,
    availabilityMutation,
    pauseMutation,
    resumeMutation,
    deleteMutation,
    resetCreditMutation,
  } = useAccounts();
  const oauth = useOauth();
  const settingsQuery = useQuery({
    queryKey: ["settings", "detail"],
    queryFn: getSettings,
  });
  const updateSettingsMutation = useMutation({
    mutationFn: (payload: SettingsUpdateRequest) => updateSettings(payload),
    onSuccess: async () => {
      toast.success("Routing strategy updated");
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["settings", "detail"] }),
        queryClient.invalidateQueries({ queryKey: ["accounts", "list"] }),
      ]);
    },
    onError: (error: Error) => {
      toast.error(error.message || "Routing strategy update failed");
    },
  });

  const importDialog = useDialogState();
  const providerDialog = useDialogState();
  const oauthDialog = useDialogState<string>();
  const deleteDialog = useDialogState<string>();
  const resetDialog = useDialogState<string>();

  const accounts = useMemo(
    () => accountsQuery.data ?? [],
    [accountsQuery.data],
  );
  const resetCreditsByAccount = useAccountResetCreditMap(accounts);
  const duplicateAccountIds = useMemo(
    () => buildDuplicateAccountIdSet(accounts),
    [accounts],
  );
  const selectedAccountId = searchParams.get("selected");

  const handleSelectAccount = useCallback(
    (accountId: string) => {
      const nextSearchParams = new URLSearchParams(searchParams);
      nextSearchParams.set("selected", accountId);
      setSearchParams(nextSearchParams);
    },
    [searchParams, setSearchParams],
  );

  const resolvedSelectedAccountId = useMemo(() => {
    if (accounts.length === 0) {
      return null;
    }
    if (
      selectedAccountId &&
      accounts.some((account) => account.accountId === selectedAccountId)
    ) {
      return selectedAccountId;
    }
    return accounts[0].accountId;
  }, [accounts, selectedAccountId]);

  const selectedAccount = useMemo(
    () =>
      resolvedSelectedAccountId
        ? (accounts.find(
            (account) => account.accountId === resolvedSelectedAccountId,
          ) ?? null)
        : null,
    [accounts, resolvedSelectedAccountId],
  );
  const resetAccount = useMemo(
    () =>
      resetDialog.data
        ? (accounts.find((account) => account.accountId === resetDialog.data) ??
          null)
        : null,
    [accounts, resetDialog.data],
  );
  const routingStrategy =
    settingsQuery.data?.routingStrategy ?? "high_waterline";

  const mutationBusy =
    importMutation.isPending ||
    createProviderMutation.isPending ||
    updatePriorityMutation.isPending ||
    updateAllFastServiceTierMutation.isPending ||
    updateSettingsMutation.isPending ||
    availabilityMutation.isPending ||
    pauseMutation.isPending ||
    resumeMutation.isPending ||
    deleteMutation.isPending ||
    resetCreditMutation.isPending;

  const mutationError =
    getErrorMessageOrNull(importMutation.error) ||
    getErrorMessageOrNull(createProviderMutation.error) ||
    getErrorMessageOrNull(updatePriorityMutation.error) ||
    getErrorMessageOrNull(updateAllFastServiceTierMutation.error) ||
    getErrorMessageOrNull(updateSettingsMutation.error) ||
    getErrorMessageOrNull(availabilityMutation.error) ||
    getErrorMessageOrNull(pauseMutation.error) ||
    getErrorMessageOrNull(resumeMutation.error) ||
    getErrorMessageOrNull(deleteMutation.error) ||
    getErrorMessageOrNull(resetCreditMutation.error);

  return (
    <div className="animate-fade-in-up space-y-6">
      {/* Page header */}
      <div className="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Accounts</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Manage imported accounts and authentication flows.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <span className="text-xs font-medium text-muted-foreground">
            Routing
          </span>
          <Select
            value={routingStrategy}
            disabled={!settingsQuery.data || updateSettingsMutation.isPending}
            onValueChange={(value) => {
              if (!settingsQuery.data) return;
              void updateSettingsMutation.mutateAsync(
                buildSettingsUpdateRequest(settingsQuery.data, {
                  routingStrategy:
                    value as DashboardSettings["routingStrategy"],
                }),
              );
            }}
          >
            <SelectTrigger
              className="h-8 w-44 text-xs"
              aria-label="Routing strategy"
            >
              <SelectValue />
            </SelectTrigger>
            <SelectContent align="end">
              <SelectItem value="high_waterline">High waterline</SelectItem>
              <SelectItem value="primary_drain">Primary drain</SelectItem>
              <SelectItem value="capacity_weighted">
                Capacity weighted
              </SelectItem>
              <SelectItem value="usage_weighted">Usage weighted</SelectItem>
            </SelectContent>
          </Select>
        </div>
      </div>

      {mutationError ? (
        <AlertMessage variant="error">{mutationError}</AlertMessage>
      ) : null}

      {!accountsQuery.data ? (
        <AccountsSkeleton />
      ) : (
        <div className="grid gap-4 lg:grid-cols-[22rem_minmax(0,1fr)]">
          <div className="rounded-xl border bg-card p-4">
            <AccountList
              accounts={accounts}
              resetCreditsByAccount={resetCreditsByAccount}
              selectedAccountId={resolvedSelectedAccountId}
              onSelect={handleSelectAccount}
              onOpenImport={() => importDialog.show()}
              onOpenOauth={() => oauthDialog.show("")}
              onOpenProvider={() => providerDialog.show()}
              onSetAllFastServiceTier={(enabled) =>
                void updateAllFastServiceTierMutation.mutateAsync(enabled)
              }
              fastServiceTierBusy={updateAllFastServiceTierMutation.isPending}
            />
          </div>

          <AccountDetail
            account={selectedAccount}
            showAccountId={
              selectedAccount
                ? duplicateAccountIds.has(selectedAccount.accountId)
                : false
            }
            busy={mutationBusy}
            onPause={(accountId) => void pauseMutation.mutateAsync(accountId)}
            onResume={(accountId) => void resumeMutation.mutateAsync(accountId)}
            onDelete={(accountId) => deleteDialog.show(accountId)}
            onReauth={(accountId) => oauthDialog.show(accountId)}
            onUseRateLimitReset={(accountId) => resetDialog.show(accountId)}
            resetBusy={resetCreditMutation.isPending}
            onUpdateRouting={(
              accountId,
              configuredPriority,
              kycEnabled,
              fastServiceTierEnabled,
              groups,
              primaryDrainPriorityEnabled,
              subscriptionRenewsAt,
            ) =>
              updatePriorityMutation
                .mutateAsync({
                  accountId,
                  configuredPriority,
                  kycEnabled,
                  fastServiceTierEnabled,
                  groups,
                  primaryDrainPriorityEnabled,
                  subscriptionRenewsAt,
                })
                .then(() => undefined)
            }
            onTestAvailability={(accountId) =>
              availabilityMutation.mutateAsync(accountId).then(() => undefined)
            }
          />
        </div>
      )}

      <ImportDialog
        open={importDialog.open}
        busy={importMutation.isPending}
        error={getErrorMessageOrNull(importMutation.error)}
        onOpenChange={importDialog.onOpenChange}
        onImport={async (file) => {
          await importMutation.mutateAsync(file);
        }}
      />

      <ApiProviderDialog
        open={providerDialog.open}
        busy={createProviderMutation.isPending}
        error={getErrorMessageOrNull(createProviderMutation.error)}
        onOpenChange={providerDialog.onOpenChange}
        onCreate={async (payload) => {
          await createProviderMutation.mutateAsync(payload);
        }}
      />

      <Suspense fallback={null}>
        <OauthDialog
          open={oauthDialog.open}
          state={oauth.state}
          onOpenChange={oauthDialog.onOpenChange}
          onStart={async (method) => {
            await oauth.start(method, oauthDialog.data || undefined);
          }}
          onComplete={async () => {
            await oauth.complete();
            await accountsQuery.refetch();
          }}
          onManualCallback={async (callbackUrl) => {
            await oauth.manualCallback(callbackUrl);
          }}
          onReset={oauth.reset}
        />
      </Suspense>

      <ConfirmDialog
        open={deleteDialog.open}
        title="Delete account"
        description="This action removes the account from the load balancer configuration."
        confirmLabel="Delete"
        cancelLabel="Cancel"
        onOpenChange={deleteDialog.onOpenChange}
        onConfirm={() => {
          if (!deleteDialog.data) {
            return;
          }
          void deleteMutation.mutateAsync(deleteDialog.data).finally(() => {
            deleteDialog.hide();
          });
        }}
      />

      <ConfirmDialog
        open={resetDialog.open}
        title="Use rate-limit reset"
        description={
          resetAccount
            ? `Consume one reset credit for ${resetAccount.displayName || resetAccount.email}.`
            : "Consume one reset credit for this account."
        }
        confirmLabel="Use reset"
        cancelLabel="Cancel"
        onOpenChange={resetDialog.onOpenChange}
        onConfirm={() => {
          if (!resetDialog.data) {
            return;
          }
          void resetCreditMutation
            .mutateAsync({ accountId: resetDialog.data })
            .finally(() => {
              resetDialog.hide();
            });
        }}
      />

      <LoadingOverlay
        visible={!!accountsQuery.data && mutationBusy}
        label="Updating accounts..."
      />
    </div>
  );
}
