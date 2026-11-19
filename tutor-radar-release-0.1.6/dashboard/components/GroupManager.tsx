'use client';

import { useCallback, useEffect, useMemo, useState } from 'react';

type AvailableGroup = {
  group_key: string;
  chat_name: string;
  monitored: boolean;
  unread: number | null;
  last_time: number | null;
};

type GroupPayload = {
  groups: AvailableGroup[];
  error: string | null;
};

export default function GroupManager({ firstRun, onSaved }: { firstRun: boolean; onSaved: () => void }) {
  const [open, setOpen] = useState(false);
  const [groups, setGroups] = useState<AvailableGroup[]>([]);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [initialSelected, setInitialSelected] = useState<Set<string>>(new Set());
  const [query, setQuery] = useState('');
  const [state, setState] = useState<'idle' | 'loading' | 'saving' | 'ready' | 'error' | 'saved'>('idle');
  const [message, setMessage] = useState('');
  const [confirmEmpty, setConfirmEmpty] = useState(false);

  const loadGroups = useCallback(async () => {
    setState('loading');
    setMessage('正在读取微信近期群聊…');
    setConfirmEmpty(false);
    try {
      const response = await fetch('http://localhost:18765/api/groups/available?limit=300', { cache: 'no-store' });
      if (!response.ok) throw new Error('request failed');
      const payload = await response.json() as GroupPayload;
      const monitored = new Set(payload.groups.filter(group => group.monitored).map(group => group.group_key));
      setGroups(payload.groups);
      setSelected(monitored);
      setInitialSelected(new Set(monitored));
      setState(payload.error ? 'error' : 'ready');
      setMessage(payload.error || `已找到 ${payload.groups.length} 个近期微信群`);
    } catch {
      setState('error');
      setMessage('暂时无法读取群聊，请确认微信已登录后重试');
    }
  }, []);

  const show = useCallback(() => {
    setOpen(true);
    setQuery('');
    void loadGroups();
  }, [loadGroups]);

  useEffect(() => {
    if (firstRun) show();
  }, [firstRun, show]);

  const toggle = (key: string) => {
    setConfirmEmpty(false);
    setState('ready');
    setMessage(`已找到 ${groups.length} 个近期微信群`);
    setSelected(current => {
      const next = new Set(current);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  const save = async () => {
    if (selected.size === 0 && initialSelected.size > 0 && !confirmEmpty) {
      setConfirmEmpty(true);
      setMessage('当前没有选择群。再次点击即可暂停全部群，已有订单不会删除。');
      return;
    }
    setState('saving');
    setMessage('正在保存监控群…');
    try {
      const response = await fetch('http://localhost:18765/api/groups/selection', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ group_keys: [...selected] }),
      });
      if (!response.ok) throw new Error('request failed');
      setInitialSelected(new Set(selected));
      setState('saved');
      setMessage(selected.size ? `已保存，后台将自动监控 ${selected.size} 个群` : '已暂停全部群，已有订单仍然保留');
      setConfirmEmpty(false);
      if (selected.size) {
        setOpen(false);
        onSaved();
      }
    } catch {
      setState('error');
      setMessage('保存失败，请刷新群聊列表后重试');
    }
  };

  const visible = useMemo(() => {
    const term = query.trim().toLocaleLowerCase('zh-CN');
    return term
      ? groups.filter(group => group.chat_name.toLocaleLowerCase('zh-CN').includes(term))
      : groups;
  }, [groups, query]);
  const changed = groups.length > 0 && (
    selected.size !== initialSelected.size
    || [...selected].some(key => !initialSelected.has(key))
  );

  return (
    <>
      <button type="button" className="manage-groups-button" onClick={show}>管理群聊</button>
      {open && <div className="group-manager-backdrop" role="presentation" onMouseDown={event => {
        if (event.target === event.currentTarget) setOpen(false);
      }}>
        <section className="group-manager" role="dialog" aria-modal="true" aria-labelledby="group-manager-title">
          <header>
            <div><p>{firstRun ? '首次使用 · 第 2 步，共 3 步' : '只读微信会话'}</p><h2 id="group-manager-title">选择要监控的家教群</h2></div>
            <button type="button" className="manager-close" aria-label="关闭群管理" onClick={() => setOpen(false)}>×</button>
          </header>
          <div className="manager-search-row">
            <input value={query} onChange={event => setQuery(event.target.value)} placeholder="搜索群名称" aria-label="搜索群名称" />
            <button type="button" onClick={() => void loadGroups()} disabled={state === 'loading'}>刷新微信群</button>
          </div>
          <div className="manager-summary"><span>{message}</span><b>已选择 {selected.size} 个</b></div>
          <div className="manager-list" aria-busy={state === 'loading'}>
            {state === 'loading' && <div className="manager-empty">正在读取近期微信群…</div>}
            {state !== 'loading' && visible.map(group => {
              const checked = selected.has(group.group_key);
              return <label key={group.group_key} className={checked ? 'selected' : ''}>
                <input type="checkbox" checked={checked} onChange={() => toggle(group.group_key)} />
                <span><strong>{group.chat_name}</strong><small>{group.monitored ? '当前正在监控' : '近期微信群'}</small></span>
                <i aria-hidden="true" />
              </label>;
            })}
            {state !== 'loading' && visible.length === 0 && <div className="manager-empty">{query.trim() ? '没有匹配的群，请试试更短的群名。' : '先在电脑版微信打开目标群，再回来点击「刷新微信群」。'}</div>}
          </div>
          <footer>
            <p>停用只会停止接收新消息，不会删除这个群的订单记录。</p>
            <div><button type="button" className="manager-cancel" onClick={() => setOpen(false)}>取消</button><button type="button" className="manager-save" disabled={state === 'loading' || state === 'saving' || state === 'saved' || !changed} onClick={() => void save()}>{state === 'saving' ? '保存中…' : confirmEmpty ? '确认暂停全部' : state === 'saved' ? '已保存' : '保存并应用'}</button></div>
          </footer>
        </section>
      </div>}
    </>
  );
}
