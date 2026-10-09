// Genuine advertised native tool declarations, Codex 0.156.1. No implementation.

declare const tools: { exec_command(args: {
  // Shell command to execute.
  cmd: string;
  // User-facing approval question for `require_escalated`; omit otherwise.
  justification?: string;
  // True runs the shell with -l/-i semantics; false disables them. Defaults to true.
  login?: boolean;
  // Output token budget. Defaults to 10000 tokens; larger requests may be capped by policy.
  max_output_tokens?: number;
  // Reusable approval prefix for `cmd`, only with `sandbox_permissions: "require_escalated"`; for example ["git", "pull"].
  prefix_rule?: Array<string>;
  // Per-command sandbox override. Defaults to `use_default`; use `require_escalated` for unsandboxed execution.
  sandbox_permissions?: "use_default" | "require_escalated";
  // Shell binary to launch. Defaults to the user's default shell.
  shell?: string;
  // True allocates a PTY for the command; false or omitted uses plain pipes.
  tty?: boolean;
  // Working directory for the command. Defaults to the turn cwd.
  workdir?: string;
  // Wait before yielding output. Defaults to 10000 ms; effective range is 250-30000 ms.
  yield_time_ms?: number;
}): Promise<{
  // Chunk identifier included when the response reports one.
  chunk_id?: string;
  // Process exit code when the command finished during this call.
  exit_code?: number;
  // Approximate token count before output truncation.
  original_token_count?: number;
  // Command output text, possibly truncated.
  output: string;
  // Session identifier to pass to write_stdin when the process is still running.
  session_id?: number;
  // Elapsed wall time spent waiting for output in seconds.
  wall_time_seconds: number;
}>; };

declare const tools: { write_stdin(args: {
  // Bytes to write to stdin. Defaults to empty, which polls without writing.
  chars?: string;
  // Output token budget. Defaults to 10000 tokens; larger requests may be capped by policy.
  max_output_tokens?: number;
  // Identifier of the running unified exec session.
  session_id: number;
  // Wait before yielding output. Non-empty writes default to 250 ms and cap at 30000 ms; empty polls wait 5000-300000 ms by default.
  yield_time_ms?: number;
}): Promise<{
  // Chunk identifier included when the response reports one.
  chunk_id?: string;
  // Process exit code when the command finished during this call.
  exit_code?: number;
  // Approximate token count before output truncation.
  original_token_count?: number;
  // Command output text, possibly truncated.
  output: string;
  // Session identifier to pass to write_stdin when the process is still running.
  session_id?: number;
  // Elapsed wall time spent waiting for output in seconds.
  wall_time_seconds: number;
}>; };
