import { createHash } from 'node:crypto';

function hash(value) {
  return createHash('sha256').update(JSON.stringify(value)).digest('hex');
}

function timestamp(value) {
  if (typeof value !== 'number' || !Number.isFinite(value)) return undefined;
  const date = new Date(value);
  return Number.isFinite(date.getTime()) ? date.toISOString() : undefined;
}

function selected(value, fields) {
  return Object.fromEntries(fields.filter((field) => value?.[field] !== undefined)
    .map((field) => [field, value[field]]));
}

function ownedJob(config, job) {
  return typeof job?.id === 'string' && job.id.trim() && job.agentId === config.targetAgentId;
}

const USER_TASK_PAYLOADS = new Set(['agentTurn', 'systemEvent', 'command', 'script']);
const RECURRING_SCHEDULES = new Set(['cron', 'every']);
const SUPPRESSED_RESULTS = new Set(['empty', 'silent', 'heartbeat', 'channel_transform']);
const CONTROL_RESULT = /^(?:HEARTBEAT_OK|NO_REPLY)(?:\s+(?:HEARTBEAT_OK|NO_REPLY))*$/i;

function text(value) {
  return typeof value === 'string' ? value.trim() : '';
}

export function registerCronCapture(api, config, retain, safeError) {
  if (config.captureCronJobs !== true) return;

  function skipped(job, reason) {
    api.logger.info(`oam-openclaw: cron skipped job=${job.id} reason=${reason}`);
  }

  function userTask(job) {
    const reason = job.payload?.kind === 'heartbeat' ? 'maintenance-payload'
      : !USER_TASK_PAYLOADS.has(job.payload?.kind) ? 'unsupported-payload'
      : !text(job.name) && !text(job.description) ? 'missing-task-description' : undefined;
    if (reason) skipped(job, reason);
    return !reason;
  }

  async function capture(job, kind, body, signal) {
    if (signal?.aborted) return;
    const key = `oam-oc-cron-${hash([config.userId, config.targetAgentId, job.id, kind, body])}`;
    const started = Date.now();
    try {
      const receipt = await retain(body, key, signal);
      api.logger.info(`oam-openclaw: cron ${kind} job=${job.id} operation=${receipt.operation_id} source=${receipt.source_id} duration_ms=${Date.now() - started}`);
    } catch (error) {
      api.logger.warn(`oam-openclaw: cron ${kind} job=${job.id} ${safeError(error)} duration_ms=${Date.now() - started}`);
    }
  }

  async function definition(job, state, signal) {
    if (!RECURRING_SCHEDULES.has(job.schedule?.kind)) {
      skipped(job, 'transient-task');
      return;
    }
    const eventTime = timestamp(job.updatedAtMs ?? job.createdAtMs);
    if (!eventTime) {
      api.logger.warn(`oam-openclaw: cron definition job=${job.id} skipped invalid timestamp`);
      return;
    }
    const content = `OpenClaw cron job definition (historical snapshot):\n${JSON.stringify({
      state: state === 'removed' ? 'removed' : job.enabled === true ? 'enabled' : 'disabled',
      name: text(job.name) || undefined, purpose: text(job.description) || undefined,
      schedule: selected(job.schedule, ['kind', 'expr', 'tz', 'at', 'everyMs']),
    })}`;
    await capture(job, 'definition', {
      input_type: 'interaction', user_id: config.userId,
      messages: [{ role: 'user', content, event_time: eventTime, learning_eligible: true }],
    }, signal);
  }

  async function finished(job, event, signal) {
    const eventTime = timestamp(event.runAtMs);
    if (!eventTime || !['ok', 'error', 'skipped'].includes(event.status)) {
      api.logger.warn(`oam-openclaw: cron outcome job=${job.id} skipped invalid run metadata`);
      return;
    }
    if (event.status !== 'ok' || text(event.error)) {
      skipped(job, 'unsuccessful-run');
      return;
    }
    if (SUPPRESSED_RESULTS.has(event.deliverySuppressionReason)) {
      skipped(job, 'suppressed-result');
      return;
    }
    const summary = text(event.summary);
    if (summary && !CONTROL_RESULT.test(summary)) {
      await capture(job, 'report', {
        input_type: 'interaction', user_id: config.userId,
        messages: [{ role: 'assistant', learning_eligible: true, event_time: eventTime,
          content: event.summary }],
      }, signal);
    } else {
      skipped(job, summary ? 'control-result' : 'routine-outcome');
    }
  }

  api.on('cron_reconciled', async (_event, ctx) => {
    if (ctx?.abortSignal?.aborted) return;
    try {
      const cron = ctx?.getCron?.();
      if (typeof cron?.list !== 'function') throw new Error('missing cron service');
      const jobs = await cron.list({ includeDisabled: true });
      if (!Array.isArray(jobs)) throw new Error('invalid cron list');
      for (const job of jobs) {
        if (ctx.abortSignal?.aborted) break;
        if (!ownedJob(config, job) || !userTask(job)) continue;
        if (job.enabled !== true) {
          skipped(job, 'disabled-snapshot');
          continue;
        }
        await definition(job, 'present', ctx.abortSignal);
        if (job.state?.lastRunAtMs !== undefined) {
          await finished(job, {
            runAtMs: job.state.lastRunAtMs, status: job.state.lastRunStatus,
            durationMs: job.state.lastDurationMs, error: job.state.lastError,
            delivered: job.state.lastDelivered, deliveryStatus: job.state.lastDeliveryStatus,
            deliveryError: job.state.lastDeliveryError,
          }, ctx.abortSignal);
        }
      }
    } catch (error) {
      api.logger.warn(`oam-openclaw: cron reconciliation ${safeError(error)}`);
    }
  });

  api.on('cron_changed', async (event, ctx) => {
    const job = event?.job;
    if (!ownedJob(config, job) || event.jobId !== job.id ||
        (event.agentId !== undefined && event.agentId !== config.targetAgentId) || !userTask(job)) return;
    if (['added', 'updated', 'removed'].includes(event.action)) {
      await definition(job, event.action === 'removed' ? 'removed' : 'present', ctx?.abortSignal);
    } else if (event.action === 'finished') {
      await finished(job, event, ctx?.abortSignal);
    }
  });
}
