type FrequencyOrder = {
  frequency_per_week?: number | null;
  schedule_text?: string | null;
};

// Display source ranges without inventing a single numeric scoring value.
const COUNT = '(?:[1-9]\\d?|[一二两三四五六七八九十])';
const COUNTS = `(?:${COUNT}\\s*[-~～—–至到]\\s*${COUNT}|一两|两三|${COUNT})`;

export function formatMapFrequency(order: FrequencyOrder): string {
  const schedule = (order.schedule_text || '').normalize('NFKC');
  const labels: string[] = [];
  const patterns = [
    new RegExp(`(?:(?:一|每(?:个)?)(?:周|星期|礼拜)|每(?:天|日|月)|隔周)\\s*(?:上课|上)?\\s*${COUNTS}\\s*次`, 'g'),
    new RegExp(`${COUNTS}\\s*次\\s*[/／]\\s*(?:周|星期|月|天)`, 'g'),
  ];
  for (const pattern of patterns) {
    for (const match of schedule.matchAll(pattern)) {
      const label = match[0].trim();
      if (!labels.includes(label)) labels.push(label);
    }
  }
  // Source wording wins over a numeric field that cannot represent a range.
  if (labels.length) return labels.join('；');

  // A range without an explicit period is still useful; never invent "每周".
  const range = schedule.match(new RegExp(`(?:${COUNT}\\s*[-~～—–至到]\\s*${COUNT}|一两|两三)\\s*次`));
  if (range) return `${range[0]}（周期未注明）`;
  const count = order.frequency_per_week;
  return typeof count === 'number' && Number.isFinite(count) && count > 0
    ? `每周 ${count} 次`
    : '频率待确认';
}
