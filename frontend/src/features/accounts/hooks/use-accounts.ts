import { useMemo } from "react";
import {
  useMutation,
  useQueries,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { toast } from "sonner";

import {
  consumeAccountRateLimitResetCredit,
  createApiProvider,
  deleteAccount,
  getAccountRateLimitResetCredits,
  getAccountTrends,
  importAccount,
  listAccounts,
  pauseAccount,
  reactivateAccount,
  testAccountAvailability,
  updateAccountRouting,
  updateAllAccountsFastServiceTier,
} from "@/features/accounts/api";
import type {
  AccountSummary,
  ApiProviderCreateRequest,
} from "@/features/accounts/schemas";

function invalidateAccountRelatedQueries(
  queryClient: ReturnType<typeof useQueryClient>,
) {
  void queryClient.invalidateQueries({ queryKey: ["accounts", "list"] });
  void queryClient.invalidateQueries({ queryKey: ["dashboard", "overview"] });
}

/**
 * Account mutation actions without the polling query.
 * Use this when you need account actions but already have account data
 * from another source (e.g. the dashboard overview query).
 */
export function useAccountMutations() {
  const queryClient = useQueryClient();

  const importMutation = useMutation({
    mutationFn: importAccount,
    onSuccess: () => {
      toast.success("Account imported");
      invalidateAccountRelatedQueries(queryClient);
    },
    onError: (error: Error) => {
      toast.error(error.message || "Import failed");
    },
  });

  const createProviderMutation = useMutation({
    mutationFn: (payload: ApiProviderCreateRequest) =>
      createApiProvider(payload),
    onSuccess: () => {
      toast.success("Provider added");
      invalidateAccountRelatedQueries(queryClient);
    },
    onError: (error: Error) => {
      toast.error(error.message || "Provider setup failed");
    },
  });

  const updatePriorityMutation = useMutation({
    mutationFn: ({
      accountId,
      configuredPriority,
      kycEnabled,
      fastServiceTierEnabled,
      primaryDrainPriorityEnabled,
      subscriptionRenewsAt,
      groups,
    }: {
      accountId: string;
      configuredPriority: number;
      kycEnabled?: boolean;
      fastServiceTierEnabled?: boolean;
      primaryDrainPriorityEnabled?: boolean;
      subscriptionRenewsAt?: string | null;
      groups?: string[];
    }) =>
      updateAccountRouting(accountId, {
        configuredPriority,
        kycEnabled,
        fastServiceTierEnabled,
        primaryDrainPriorityEnabled,
        subscriptionRenewsAt,
        groups,
      }),
    onSuccess: () => {
      toast.success("Routing settings updated");
      invalidateAccountRelatedQueries(queryClient);
    },
    onError: (error: Error) => {
      toast.error(error.message || "Priority update failed");
    },
  });

  const updateAllFastServiceTierMutation = useMutation({
    mutationFn: updateAllAccountsFastServiceTier,
    onSuccess: (result) => {
      toast.success(
        `Fast mode ${result.enabled ? "enabled" : "disabled"} for ${result.updatedCount} accounts`,
      );
      invalidateAccountRelatedQueries(queryClient);
    },
    onError: (error: Error) => {
      toast.error(error.message || "Fast mode update failed");
    },
  });

  const availabilityMutation = useMutation({
    mutationFn: testAccountAvailability,
    onSuccess: (result) => {
      const detail = `${result.passedCount}/${result.testedCount} passed`;
      if (result.status === "active") {
        toast.success(`Availability check passed (${detail})`);
      } else {
        toast.warning(
          `Availability check returned ${result.status} (${detail})`,
        );
      }
      invalidateAccountRelatedQueries(queryClient);
    },
    onError: (error: Error) => {
      toast.error(error.message || "Availability check failed");
    },
  });

  const pauseMutation = useMutation({
    mutationFn: pauseAccount,
    onSuccess: () => {
      toast.success("Account paused");
      invalidateAccountRelatedQueries(queryClient);
    },
    onError: (error: Error) => {
      toast.error(error.message || "Pause failed");
    },
  });

  const resumeMutation = useMutation({
    mutationFn: reactivateAccount,
    onSuccess: () => {
      toast.success("Account resumed");
      invalidateAccountRelatedQueries(queryClient);
    },
    onError: (error: Error) => {
      toast.error(error.message || "Resume failed");
    },
  });

  const deleteMutation = useMutation({
    mutationFn: deleteAccount,
    onSuccess: () => {
      toast.success("Account deleted");
      invalidateAccountRelatedQueries(queryClient);
    },
    onError: (error: Error) => {
      toast.error(error.message || "Delete failed");
    },
  });

  const resetCreditMutation = useMutation({
    mutationFn: ({
      accountId,
      idempotencyKey,
    }: {
      accountId: string;
      idempotencyKey?: string;
    }) => consumeAccountRateLimitResetCredit(accountId, idempotencyKey),
    onSuccess: (result) => {
      if (result.outcome === "reset" || result.outcome === "already_redeemed") {
        const suffix =
          result.windowsReset > 0
            ? ` (${result.windowsReset} window${result.windowsReset === 1 ? "" : "s"})`
            : "";
        toast.success(`Rate-limit reset applied${suffix}`);
      } else if (result.outcome === "no_credit") {
        toast.warning("No reset credits are available for this account");
      } else {
        toast.warning("No eligible usage window needs a reset");
      }
      invalidateAccountRelatedQueries(queryClient);
      void queryClient.invalidateQueries({
        queryKey: ["accounts", "reset-credits", result.accountId],
      });
    },
    onError: (error: Error) => {
      toast.error(error.message || "Rate-limit reset failed");
    },
  });

  return {
    importMutation,
    createProviderMutation,
    updatePriorityMutation,
    updateAllFastServiceTierMutation,
    availabilityMutation,
    pauseMutation,
    resumeMutation,
    deleteMutation,
    resetCreditMutation,
  };
}

export function useAccountTrends(accountId: string | null) {
  return useQuery({
    queryKey: ["accounts", "trends", accountId],
    queryFn: () => getAccountTrends(accountId!),
    enabled: !!accountId,
    staleTime: 5 * 60_000,
    refetchInterval: 5 * 60_000,
    refetchIntervalInBackground: false,
  });
}

export function useAccountResetCredits(
  accountId: string | null,
  enabled: boolean,
) {
  return useQuery({
    queryKey: ["accounts", "reset-credits", accountId],
    queryFn: () => getAccountRateLimitResetCredits(accountId!),
    enabled: Boolean(accountId) && enabled,
    staleTime: 60_000,
    refetchInterval: 60_000,
    refetchIntervalInBackground: false,
    retry: 1,
  });
}

export function useAccountResetCreditMap(accounts: AccountSummary[]) {
  const oauthAccounts = useMemo(
    () => accounts.filter((account) => account.providerKind !== "api_key"),
    [accounts],
  );
  const results = useQueries({
    queries: oauthAccounts.map((account) => ({
      queryKey: ["accounts", "reset-credits", account.accountId],
      queryFn: () => getAccountRateLimitResetCredits(account.accountId),
      staleTime: 60_000,
      refetchInterval: 60_000,
      refetchIntervalInBackground: false,
      retry: 1,
    })),
  });

  return useMemo(() => {
    const counts: Record<string, number> = {};
    results.forEach((result, index) => {
      const account = oauthAccounts[index];
      if (account && result.data) {
        counts[account.accountId] = result.data.availableCount;
      }
    });
    return counts;
  }, [oauthAccounts, results]);
}

export function useAccounts() {
  const accountsQuery = useQuery({
    queryKey: ["accounts", "list"],
    queryFn: listAccounts,
    select: (data) => data.accounts,
    refetchInterval: 30_000,
    refetchIntervalInBackground: false,
  });

  const mutations = useAccountMutations();

  return { accountsQuery, ...mutations };
}
