// Code Mode adapter fragment, inserted after the existing exec call.
// Keep the command, trigger state, instructions and model configuration unchanged.
const details = result?.details ?? result?.result?.details ?? result?.result ?? result;
if (!details || typeof details !== "object")
  throw new Error("mail_trigger_exec_contract_missing");
if (details.timedOut === true || details.noOutputTimedOut === true ||
    ["overall-timeout", "no-output-timeout"].includes(details.failureKind) ||
    ["overall-timeout", "no-output-timeout"].includes(details.exitReason))
  throw new Error("mail_trigger_exec_timeout; inspect durable queue before retry");
if (details.status === "running")
  throw new Error("mail_trigger_exec_incomplete; reconcile process before retry");
if (result?.isError || details.status === "failed" || details.exitSignal != null)
  throw new Error("mail_trigger_exec_failed; inspect durable queue before retry");
const code = details.exitCode ?? details.exit_code ?? result?.exitCode;
if (typeof code !== "number" || !Number.isInteger(code))
  throw new Error("mail_trigger_exec_exit_missing");
if (code !== 0)
  throw new Error("mail_trigger_exec_exit_" + code);
const output = details.aggregated ?? details.stdout ?? details.output ?? result?.aggregated ??
  result?.content?.find(x => x.type === "text")?.text;
if (typeof output !== "string")
  throw new Error("mail_trigger_exec_output_missing");
