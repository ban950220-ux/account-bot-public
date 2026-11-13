type WebMcpTool = {
  name: string;
  title?: string;
  description: string;
  inputSchema: object;
  annotations?: { readOnlyHint?: boolean; untrustedContentHint?: boolean };
  execute(input: unknown): unknown | Promise<unknown>;
};

interface Document {
  readonly modelContext?: {
    registerTool(tool: WebMcpTool, options?: { signal?: AbortSignal }): void | Promise<void>;
  };
}
