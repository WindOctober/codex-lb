import { useMemo, useState } from "react";
import { ArrowDown, ArrowUp, Plus, X } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useAccounts } from "@/features/accounts/hooks/use-accounts";
import { cn } from "@/lib/utils";

const GROUP_NAME_PATTERN = /^[a-z0-9][a-z0-9._:-]{0,127}$/;
const RESERVED_GROUPS = ["general", "plus", "pro", "kyc"];
const PROVIDER_GROUP_PREFIX = "provider:";

export type GroupSelectorProps = {
  value: string[];
  onChange: (value: string[]) => void;
  emptyLabel?: string;
  addPlaceholder?: string;
  ordered?: boolean;
};

function normalizeGroupName(value: string): string {
  return value.trim().toLowerCase();
}

function uniqueGroups(groups: string[]): string[] {
  const seen = new Set<string>();
  const normalized: string[] = [];
  for (const group of groups) {
    const value = normalizeGroupName(group);
    if (!value || seen.has(value)) continue;
    seen.add(value);
    normalized.push(value);
  }
  return normalized;
}

function validateGroupName(group: string): string | null {
  if (!group) return "Enter a group name first.";
  if (group.includes(",")) return "Add one group at a time; commas are not used here.";
  if (!GROUP_NAME_PATTERN.test(group)) {
    return "Use lowercase letters, numbers, dots, underscores, colons, or hyphens.";
  }
  return null;
}

function sortGroupOptions(groups: string[]): string[] {
  return [...groups].sort((a, b) => {
    const reservedA = RESERVED_GROUPS.indexOf(a);
    const reservedB = RESERVED_GROUPS.indexOf(b);
    if (reservedA >= 0 || reservedB >= 0) {
      return (reservedA >= 0 ? reservedA : RESERVED_GROUPS.length) - (reservedB >= 0 ? reservedB : RESERVED_GROUPS.length);
    }
    const providerA = a.startsWith(PROVIDER_GROUP_PREFIX);
    const providerB = b.startsWith(PROVIDER_GROUP_PREFIX);
    if (providerA !== providerB) return providerA ? -1 : 1;
    return a.localeCompare(b);
  });
}

export function GroupSelector({
  value,
  onChange,
  emptyLabel = "All groups",
  addPlaceholder = "Add group, e.g. paid",
  ordered = false,
}: GroupSelectorProps) {
  const { accountsQuery } = useAccounts();
  const [draft, setDraft] = useState("");
  const [error, setError] = useState<string | null>(null);
  const selectedGroups = useMemo(() => uniqueGroups(value), [value]);
  const selectedSet = useMemo(() => new Set(selectedGroups), [selectedGroups]);

  const options = useMemo(() => {
    const groups = new Set<string>(RESERVED_GROUPS);
    for (const account of accountsQuery.data ?? []) {
      for (const group of account.groups ?? []) {
        const value = normalizeGroupName(group);
        if (value) groups.add(value);
      }
    }
    for (const group of selectedGroups) {
      groups.add(group);
    }
    return sortGroupOptions([...groups]);
  }, [accountsQuery.data, selectedGroups]);

  const addGroup = (rawGroup: string) => {
    const group = normalizeGroupName(rawGroup);
    const validationError = validateGroupName(group);
    if (validationError) {
      setError(validationError);
      return;
    }
    setError(null);
    setDraft("");
    if (selectedSet.has(group)) return;
    onChange([...selectedGroups, group]);
  };

  const removeGroup = (group: string) => {
    onChange(selectedGroups.filter((current) => current !== group));
  };

  const moveGroup = (group: string, direction: -1 | 1) => {
    const index = selectedGroups.indexOf(group);
    const nextIndex = index + direction;
    if (index < 0 || nextIndex < 0 || nextIndex >= selectedGroups.length) return;
    const next = [...selectedGroups];
    [next[index], next[nextIndex]] = [next[nextIndex], next[index]];
    onChange(next);
  };

  const availableOptions = options.filter((group) => !selectedSet.has(group));

  return (
    <div className="space-y-2">
      <div className="flex gap-2">
        <Input
          value={draft}
          onChange={(event) => {
            setDraft(event.target.value);
            setError(null);
          }}
          onKeyDown={(event) => {
            if (event.key === "Enter") {
              event.preventDefault();
              addGroup(draft);
            }
          }}
          placeholder={addPlaceholder}
          autoComplete="off"
        />
        <Button type="button" variant="outline" size="sm" className="h-9" onClick={() => addGroup(draft)}>
          <Plus className="size-3.5" />
          Add
        </Button>
      </div>

      {error ? <p className="text-xs text-destructive">{error}</p> : null}

      {selectedGroups.length > 0 ? (
        <div className="flex flex-wrap gap-1.5">
          {selectedGroups.map((group, index) => (
            <Badge key={group} variant="secondary" className="gap-1.5 px-2 py-1 text-xs">
              {ordered ? <span className="text-muted-foreground">{index + 1}.</span> : null}
              {group}
              {ordered ? (
                <span className="ml-0.5 inline-flex gap-0.5">
                  <button
                    type="button"
                    className="rounded-sm hover:text-foreground disabled:opacity-40"
                    disabled={index === 0}
                    onClick={() => moveGroup(group, -1)}
                    aria-label={`Move ${group} earlier`}
                  >
                    <ArrowUp className="size-3" />
                  </button>
                  <button
                    type="button"
                    className="rounded-sm hover:text-foreground disabled:opacity-40"
                    disabled={index === selectedGroups.length - 1}
                    onClick={() => moveGroup(group, 1)}
                    aria-label={`Move ${group} later`}
                  >
                    <ArrowDown className="size-3" />
                  </button>
                </span>
              ) : null}
              <button
                type="button"
                className="ml-0.5 rounded-sm hover:text-foreground"
                onClick={() => removeGroup(group)}
                aria-label={`Remove ${group}`}
              >
                <X className="size-3" />
              </button>
            </Badge>
          ))}
        </div>
      ) : (
        <p className="rounded-md border border-dashed px-3 py-2 text-xs text-muted-foreground">{emptyLabel}</p>
      )}

      {availableOptions.length > 0 ? (
        <div className="space-y-1">
          <p className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">Options</p>
          <div className="flex flex-wrap gap-1">
            {availableOptions.map((group) => (
              <button
                key={group}
                type="button"
                className={cn(
                  "rounded-full border px-2 py-0.5 text-xs transition-colors hover:bg-accent hover:text-accent-foreground",
                  group === "kyc"
                    ? "border-amber-400/60 bg-amber-500/10"
                    : group === "pro"
                      ? "border-violet-400/60 bg-violet-500/10"
                    : group === "general"
                      ? "border-sky-400/60 bg-sky-500/10"
                      : group.startsWith(PROVIDER_GROUP_PREFIX)
                        ? "border-emerald-400/60 bg-emerald-500/10"
                      : "border-border",
                )}
                onClick={() => addGroup(group)}
              >
                {group}
              </button>
            ))}
          </div>
        </div>
      ) : null}
    </div>
  );
}
