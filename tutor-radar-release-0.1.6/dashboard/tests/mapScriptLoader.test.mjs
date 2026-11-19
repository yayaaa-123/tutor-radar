import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createMapScriptLoader, MAP_SCRIPT_ATTEMPTS } from '../components/mapScriptLoader.ts';

function fixture(outcomes) {
  let sdk;
  const scripts = [];
  const load = createMapScriptLoader({
    getSDK: () => sdk,
    timeoutMs: 10,
    retryDelaysMs: [0, 0],
    createScript: () => ({
      onload: null, onerror: null, removed: false,
      remove() { this.removed = true; },
    }),
    appendScript: script => {
      scripts.push(script);
      const outcome = outcomes.shift();
      if (outcome === 'throw') throw new Error('blocked');
      if (outcome === 'timeout') return;
      queueMicrotask(() => {
        if (outcome === 'success') {
          sdk = { ready: true };
          script.onload?.();
        } else if (outcome === 'empty') {
          script.onload?.();
        } else {
          script.onerror?.();
        }
      });
    },
  });
  return { load, scripts };
}

test('concurrent consumers share one request and success reuses the SDK', async () => {
  const { load, scripts } = fixture(['success']);
  const first = load('test://map');
  assert.equal(first, load('test://map'));
  const sdk = await first;
  assert.equal(await load('test://map'), sdk);
  assert.equal(scripts.length, 1);
  assert.equal(scripts[0].removed, false);
  assert.equal(scripts[0].onload, null);
  assert.equal(scripts[0].onerror, null);
});

test('transient error retries with fresh scripts and cleans failed nodes', async () => {
  const { load, scripts } = fixture(['error', 'success']);
  assert.deepEqual(await load('test://map'), { ready: true });
  assert.equal(scripts.length, 2);
  assert.equal(scripts[0].removed, true);
  assert.equal(scripts[0].onerror, null);
});

test('stops after three attempts; manual reload can start a new successful cycle', async () => {
  const { load, scripts } = fixture(['error', 'error', 'error', 'success']);
  await assert.rejects(load('test://map?key=private'), /已尝试 3 次/);
  assert.equal(scripts.length, MAP_SCRIPT_ATTEMPTS);
  assert.ok(scripts.every(script => script.removed));
  await new Promise(resolve => setTimeout(resolve, 20));
  assert.equal(scripts.length, MAP_SCRIPT_ATTEMPTS);
  assert.deepEqual(await load('test://map'), { ready: true });
  assert.equal(scripts.length, MAP_SCRIPT_ATTEMPTS + 1);
});

test('silent requests time out, clean handlers, and allow another cycle', async () => {
  const { load, scripts } = fixture(['timeout', 'timeout', 'timeout', 'success']);
  await assert.rejects(load('test://map'), /加载超时.*已尝试 3 次/);
  assert.ok(scripts.every(script => script.removed && !script.onload && !script.onerror));
  assert.deepEqual(await load('test://map'), { ready: true });
});

test('onload without a usable SDK is retried, not cached as success', async () => {
  const { load, scripts } = fixture(['empty', 'success']);
  await load('test://map');
  assert.equal(scripts.length, 2);
  assert.equal(scripts[0].removed, true);
});

test('append errors are bounded, cleaned, and do not expose request secrets', async () => {
  const { load, scripts } = fixture(['throw', 'throw', 'throw', 'success']);
  await assert.rejects(load('test://map?key=private'), error => {
    assert.match(error.message, /已尝试 3 次/);
    assert.doesNotMatch(error.message, /private|test:\/\//);
    return true;
  });
  assert.ok(scripts.every(script => script.removed));
  await load('test://map');
});
