import { useEffect, useRef, useState } from 'react';

const url = 'http://localhost:18765/api/setup/model';
const messages: Record<string, string> = {
  idle: '',
  ready: '模型已准备好，运行中的雷达会在下一轮检查时启用。',
  ollama_required: '这台电脑还没有 Ollama。打开官方下载页，运行 Windows 安装器，装好后回来点「重新检测」。',
  error: '模型准备失败，请检查目录是否可写、磁盘空间和 Ollama 后重试。',
  checking: '正在检查 Ollama 模型清单…',
  download_required: 'Ollama 未列出 qwen3.5:4b。可先检查模型目录，或确认下载。',
};

export default function LocalModelSetup() {
  const dialog = useRef<HTMLDialogElement>(null);
  const [state, setState] = useState({ status: 'idle', percent: 0, directory: '' });
  const [directory, setDirectory] = useState('');
  useEffect(() => {
    async function refresh() {
      try {
        const response = await fetch(url);
        if (response.ok) {
          const result = await response.json();
          setState(result);
          setDirectory(previous => previous || result.directory || result.suggested_directory || '');
        }
      } catch { /* Local API is starting. */ }
    }
    void refresh();
    const timer = window.setInterval(refresh, 2000);
    return () => window.clearInterval(timer);
  }, []);
  async function request(action: 'check' | 'confirm_download' | 'browse' | 'install') {
      try {
        if (action === 'check' || action === 'confirm_download') setState({ status: 'checking', percent: 0, directory });
        const response = await fetch(url, {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action, directory }),
        });
        if (!response.ok) throw new Error();
        if (action === 'browse' || action === 'check') {
          const result = await response.json();
          if (result.directory) setDirectory(result.directory);
        }
      } catch {
        setState({ status: 'error', percent: 0, directory });
      }
  }
  const busy = ['checking', 'working'].includes(state.status);
  return <>
    <button className="manage-groups-button" onClick={() => dialog.current?.showModal()}>本地模型（可选）</button>
    <dialog ref={dialog} className="profile-dialog model-wizard" aria-labelledby="model-title">
    <header><div><p className="section-label">可选增强</p><h2 id="model-title">设置本地模型</h2></div>
      <button className="manager-close" aria-label="关闭模型设置" onClick={() => dialog.current?.close()}>×</button></header>
    <p className="profile-description">模型可补全格式不规则的订单，有机会找到更多符合条件的单，并补充推荐理由。基础筛单和固定评分可直接使用，模型在你的电脑上运行。</p>
    <div className="model-setup">
    <label>1. 选择模型保存位置<input value={directory} disabled={busy} onChange={event => setDirectory(event.target.value)} placeholder="例如 D:\\OllamaModels" /></label>
    <p>已有模型就选原来的 Ollama 模型文件夹。第一次使用可以选一个空文件夹。</p>
    <button className="manage-groups-button" disabled={busy} onClick={() => void request('browse')}>选择文件夹</button>
    <strong>2. 检查运行工具和已有模型</strong>
    <button className="manage-groups-button" disabled={busy || !directory.trim()} onClick={() => void request('check')}>{state.status === 'ollama_required' ? '重新检测' : '开始检测'}</button>
    <span role="status">{state.status === 'working' ? ` 正在下载模型 ${state.percent}%` : messages[state.status]}</span>
    {state.status === 'working' && <progress value={state.percent} max={100} aria-label="模型下载进度" />}
    {state.status === 'download_required' && <>
      <strong>3. 确认下载</strong>
      <p>预计下载数 GB，保存到 <strong>{state.directory}</strong>。改了目录请先重新检测。</p>
      <button className="manage-groups-button" disabled={directory.trim() !== state.directory} onClick={() => {
      if (window.confirm(`下载 qwen3.5:4b 约需数 GB，将保存到 ${state.directory}。确定开始吗？`)) {
        void request('confirm_download');
      }
    }}>确认下载模型</button></>}
    {state.status === 'ollama_required' && <button className="manage-groups-button" onClick={() => {
      if (window.confirm('打开 Ollama 官方 Windows 下载页？安装完成后回到雷达重新检测。')) void request('install');
    }}>前往安装 Ollama</button>}
    </div>
    <footer><button className="manager-cancel" onClick={() => dialog.current?.close()}>{state.status === 'working' ? '在后台继续' : state.status === 'ready' ? '完成' : '暂时跳过'}</button></footer>
    </dialog>
  </>;
}
