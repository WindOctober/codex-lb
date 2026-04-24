import { useState } from "react";
import type { FormEvent } from "react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import type { ApiProviderCreateRequest } from "@/features/accounts/schemas";

export type ApiProviderDialogProps = {
  open: boolean;
  busy: boolean;
  error: string | null;
  onOpenChange: (open: boolean) => void;
  onCreate: (payload: ApiProviderCreateRequest) => Promise<void>;
};

export function ApiProviderDialog({
  open,
  busy,
  error,
  onOpenChange,
  onCreate,
}: ApiProviderDialogProps) {
  const [name, setName] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [priority, setPriority] = useState("100");

  const reset = () => {
    setName("");
    setBaseUrl("");
    setApiKey("");
    setPriority("100");
  };

  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    await onCreate({
      name,
      baseUrl,
      apiKey,
      priority: Number.parseInt(priority, 10),
    });
    reset();
    onOpenChange(false);
  };

  const priorityValue = Number.parseInt(priority, 10);
  const canSubmit =
    name.trim().length > 0 &&
    baseUrl.trim().length > 0 &&
    apiKey.trim().length > 0 &&
    Number.isInteger(priorityValue) &&
    priorityValue >= 0;

  return (
    <Dialog
      open={open}
      onOpenChange={(nextOpen) => {
        onOpenChange(nextOpen);
        if (!nextOpen) {
          reset();
        }
      }}
    >
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Add API Provider</DialogTitle>
          <DialogDescription>
            Register an OpenAI-compatible upstream with an API key. The server probes the models endpoint before saving it.
          </DialogDescription>
        </DialogHeader>

        <form className="space-y-4" onSubmit={handleSubmit}>
          <div className="space-y-2">
            <Label htmlFor="provider-name">Name</Label>
            <Input
              id="provider-name"
              placeholder="OpenRouter"
              value={name}
              onChange={(event) => setName(event.target.value)}
            />
          </div>

          <div className="space-y-2">
            <Label htmlFor="provider-base-url">Base URL</Label>
            <Input
              id="provider-base-url"
              placeholder="https://api.openai.com/v1"
              value={baseUrl}
              onChange={(event) => setBaseUrl(event.target.value)}
            />
          </div>

          <div className="space-y-2">
            <Label htmlFor="provider-api-key">API key</Label>
            <Input
              id="provider-api-key"
              type="password"
              autoComplete="off"
              value={apiKey}
              onChange={(event) => setApiKey(event.target.value)}
            />
          </div>

          <div className="space-y-2">
            <Label htmlFor="provider-priority">Priority</Label>
            <Input
              id="provider-priority"
              type="number"
              min={0}
              max={100000}
              value={priority}
              onChange={(event) => setPriority(event.target.value)}
            />
            <p className="text-xs text-muted-foreground">Lower values are tried before larger values.</p>
          </div>

          {error ? (
            <p className="rounded-md border border-destructive/30 bg-destructive/10 px-2 py-1 text-xs text-destructive">
              {error}
            </p>
          ) : null}

          <DialogFooter>
            <Button type="submit" disabled={busy || !canSubmit}>
              Add Provider
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
