import assert from 'node:assert/strict';
import { test } from 'node:test';
import { formatMapFrequency } from '../components/mapFrequency.ts';

test('live weekly ranges are displayed instead of pending', () => {
  for (const [schedule, expected] of [
    ['一周1~2次，一次两小时', '一周1~2次'],
    ['一周2-3次，具体看试课安排', '一周2-3次'],
    ['每周一到两次，周六下午', '每周一到两次'],
    ['每个星期上课1至2次，每次两小时', '每个星期上课1至2次'],
    ['一周一两次，每次1.5小时', '一周一两次'],
    ['每周１～２次，周末', '每周1~2次'],
    ['1-2次/周，每次2小时', '1-2次/周'],
  ]) {
    assert.equal(formatMapFrequency({ schedule_text: schedule, frequency_per_week: null }), expected);
  }
});

test('a source range overrides a stale single numeric field', () => {
  assert.equal(formatMapFrequency({ schedule_text: '一周1~2次，一次两小时', frequency_per_week: 1 }), '一周1~2次');
});

test('single frequencies still work from either source or typed fields', () => {
  assert.equal(formatMapFrequency({ schedule_text: '开学后一周两次，每次两小时' }), '一周两次');
  assert.equal(formatMapFrequency({ frequency_per_week: 2 }), '每周 2 次');
});

test('does not confuse lesson duration, monthly frequency or alternate weeks with weekly frequency', () => {
  assert.equal(formatMapFrequency({ schedule_text: '每次1-2小时，周末协商' }), '频率待确认');
  assert.equal(formatMapFrequency({ schedule_text: '每月1-2次，每次两小时' }), '每月1-2次');
  assert.equal(formatMapFrequency({ schedule_text: '隔周一次，周六早上' }), '隔周一次');
  assert.equal(formatMapFrequency({ schedule_text: '1到两次，具体安排另议' }), '1到两次（周期未注明）');
});

test('retains multiple source frequencies instead of choosing one silently', () => {
  assert.equal(formatMapFrequency({ schedule_text: '暑假每周两次，开学每周一次' }), '每周两次；每周一次');
});

test('only genuinely absent frequency remains pending', () => {
  for (const order of [{}, { frequency_per_week: null }, { frequency_per_week: NaN }, { schedule_text: '周末商议' }]) {
    assert.equal(formatMapFrequency(order), '频率待确认');
  }
});
