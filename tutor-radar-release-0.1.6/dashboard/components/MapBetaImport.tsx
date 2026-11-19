import { useRef, useState } from 'react';

export const MAP_BETA_RESULT = 'tutor-radar-map-beta-result';

export default function MapBetaImport() {
  const input = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');

  async function importFile(file: File) {
    setBusy(true);
    setMessage('正在验证公交查询…');
    try {
      if (file.size > 65536) throw new Error('配置文件过大。');
      let body: unknown;
      try {
        body = JSON.parse(await file.text());
      } catch {
        throw new Error('请选择有效的地图配置 JSON 文件。');
      }
      const response = await fetch('http://localhost:18765/api/map/import-beta', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (response.status === 422) throw new Error('公交试算失败，请检查 Web 服务 Key、网络和额度。');
      if (!response.ok) throw new Error('导入失败，请检查配置格式和本地服务。');
      const result = await response.json() as { transit_minutes: number };
      // Reload the document so the AMap SDK cannot reuse the previous JS key.
      sessionStorage.setItem(MAP_BETA_RESULT, `配置已导入，示例公交约 ${result.transit_minutes} 分钟。底图加载结果见下方地图。`);
      window.location.reload();
    } catch (error) {
      setMessage(error instanceof TypeError ? '无法连接本地地图服务。' :
        error instanceof Error ? error.message : '地图配置导入失败。');
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <input ref={input} type="file" accept=".json,application/json" hidden onChange={event => {
        const file = event.currentTarget.files?.[0];
        event.currentTarget.value = '';
        if (file) void importFile(file);
      }} />
      <button className="manage-groups-button" disabled={busy} onClick={() => input.current?.click()}>
        导入内测地图配置
      </button>
      {message && <span role="status">{message}</span>}
    </>
  );
}
