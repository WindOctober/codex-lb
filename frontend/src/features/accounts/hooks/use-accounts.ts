import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";

import {
  createApiProvider,
  deleteAccount,
  getAccountTrends,
  importAccount,
  listAccounts,
  pauseAccount,
  reactivateAccount,
  testAccountAvailability,
  updateAccountRouting,
} from "@/features/accounts/api";
import type { ApiProviderCreateRequest } from "@/features/accounts/schemas";

function invalidateAccountRelatedQueries(queryClient: ReturnType<typeof useQueryClient>) {
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
    mutationFn: (payload: ApiProviderCreateRequest) => createApiProvider(payload),
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
    }: {
      accountId: string;
      configuredPriority: number;
      kycEnabled?: boolean;
    }) => updateAccountRouting(accountId, { configuredPriority, kycEnabled }),
    onSuccess: () => {
      toast.success("Routing settings updated");
      invalidateAccountRelatedQueries(queryClient);
    },
    onError: (error: Error) => {
      toast.error(error.message || "Priority update failed");
    },
  });

  const availabilityMutation = useMutation({
    mutationFn: testAccountAvailability,
    onSuccess: (result) => {
      const detail = `${result.passedCount}/${result.testedCount} passed`;
      if (result.status === "active") {
        toast.success(`Availability check passed (${detail})`);
      } else {
        toast.warning(`Availability check returned ${result.status} (${detail})`);
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

  return {
    importMutation,
    createProviderMutation,
    updatePriorityMutation,
    availabilityMutation,
    pauseMutation,
    resumeMutation,
    deleteMutation,
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
