'use client';

import { useEffect, useMemo, useState } from 'react';
import CandidateMap from '../components/CandidateMap';
import GroupManager from '../components/GroupManager';
import MapBetaImport, { MAP_BETA_RESULT } from '../components/MapBetaImport';
import RadarSetup from '../components/RadarSetup';
import LocalModelSetup from '../components/LocalModelSetup';

type Candidate = {
  candidate_key?: string;
  chat_id?: string;
  chat_name?: string;
  captured_at: string | null;
  source: 'live' | 'playback' | 'saved';
  order: {
    order_id: string; address: string; district: string; subjects: string[];
    subject_text: string; student_text: string; current_level: string | null;
    requirements_text: string; schedule_text: string; pay_text: string;
    hourly_rate_min: number | null; hourly_rate_max: number | null; trial_time: string | null;
    frequency_per_week?: number | null;
    raw_text?: string | null;
  };
  filter: { status: 'matched' | 'review'; priority: number; reasons: string[] };
  commute?: { status: string; origin?: { location?: string }; destination?: { location?: string }; route?: { duration_minutes?: number; walking_distance_meters?: number }; error?: string } | null;
  score?: {
    version: string; total_score: number; recommendation: 'S' | 'A' | 'B' | 'C';
    component_scores: {
      ability: { score: number; max_score: number }; earnings: { score: number; max_score: number };
      commute: { score: number; max_score: number }; long_term: { score: number; max_score: number };
    };
    economics: { effective_hourly_min: number | null; effective_hourly_max: number | null; total_time_hours: number | null };
    recommendation_reasons: string[]; non_recommendation_reasons: string[];
    grade_caps: { grade: string; reason: string }[];
  } | null;
  recognition?: { status: string; accepted_fields?: { name: string }[]; missing_fields?: string[] } | null;
  model_analysis?: { status: string; summary?: string; fit_reasons?: string[]; risks?: string[]; questions?: string[] } | null;
};

type DashboardData = {
  generated_at: string;
  health: { status: string; chat_name: string; checked_at: string; messages_seen: number; orders_extracted: number; candidates_found: number; orders_rejected: number; consecutive_errors: number; error: string | null };
  live_summary: { messages: number; orders: number; candidates: number; rejected: number };
  playback_summary: { messages: number; orders: number; candidates: number; rejected: number };
  candidates: Candidate[];
  rejected_candidates: Candidate[];
  information_missing_count: number;
  groups: { chat_id: string; chat_name: string; monitored: boolean; status: string; messages: number; orders: number; candidates_found: number; orders_rejected: number; candidates: number; rejected: number; information_missing: number }[];
};

const fallback: DashboardData = {
  generated_at: '', health: { status: 'connecting', chat_name: '家教群', checked_at: '', messages_seen: 0, orders_extracted: 0, candidates_found: 0, orders_rejected: 0, consecutive_errors: 0, error: null },
  live_summary: { messages: 0, orders: 0, candidates: 0, rejected: 0 }, playback_summary: { messages: 0, orders: 0, candidates: 0, rejected: 0 },
  candidates: [],
  rejected_candidates: [],
  information_missing_count: 0,
  groups: [],
};

const filters = ['全部', 'S', 'A', 'B', 'C', '待确认', '已拒绝'];
const formatPay = (candidate: Candidate) => candidate.order.pay_text || (candidate.order.hourly_rate_min ? `${candidate.order.hourly_rate_min} 元/小时` : '薪酬待确认');
const checkedText = (value?: string) => value ? new Date(value).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' }) : '等待连接';
const sourceText = (source: Candidate['source']) => source === 'live' ? '实时' : source === 'saved' ? '已保存' : '回放';
const commuteText = (candidate: Candidate) => candidate.commute?.status === 'ok' && candidate.commute.route?.duration_minutes
  ? `公交 ${candidate.commute.route.duration_minutes} 分钟`
  : candidate.commute?.status === 'error' ? '通勤查询失败' : '通勤待查询';

export default function Home() {
  const [data, setData] = useState(fallback);
  const [filter, setFilter] = useState('全部');
  const [view, setView] = useState<'list' | 'map'>('list');
  const [mapImportResult, setMapImportResult] = useState('');
  useEffect(() => {
    const result = sessionStorage.getItem(MAP_BETA_RESULT);
    if (result) {
      sessionStorage.removeItem(MAP_BETA_RESULT);
      setMapImportResult(result);
      setView('map');
    }
  }, []);
  const [apiError, setApiError] = useState(false);
  const [firstRun, setFirstRun] = useState<'choose' | 'verify' | null>(null);
  const [selectedChatId, setSelectedChatId] = useState('');
  const [groupView, setGroupView] = useState<'active' | 'history'>('active');

  const mutateCandidate = async (candidate: Candidate, action: 'reject' | 'restore') => {
    const scopedKey = candidate.candidate_key || candidate.order.order_id;
    try {
      const response = await fetch('http://localhost:18765/api/candidates/reject', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ candidate_key: scopedKey, action }),
      });
      if (!response.ok) throw new Error('request failed');
      setData(current => {
        const source = action === 'reject' ? current.candidates : current.rejected_candidates;
        const moved = source.find(item => (item.candidate_key || item.order.order_id) === scopedKey);
        if (!moved) return current;
        return action === 'reject'
          ? { ...current, candidates: current.candidates.filter(item => (item.candidate_key || item.order.order_id) !== scopedKey), rejected_candidates: [moved, ...current.rejected_candidates] }
          : { ...current, rejected_candidates: current.rejected_candidates.filter(item => (item.candidate_key || item.order.order_id) !== scopedKey), candidates: [moved, ...current.candidates] };
      });
    } catch { setApiError(true); }
  };

  useEffect(() => {
    let active = true;
    const refresh = async () => {
      try {
        const response = await fetch('http://localhost:18765/api/dashboard', { cache: 'no-store' });
        if (!response.ok) throw new Error('dashboard api unavailable');
        const next = await response.json() as DashboardData;
        if (active) { setData(next); setApiError(false); }
      } catch { if (active) setApiError(true); }
    };
    refresh();
    const timer = window.setInterval(refresh, 5000);
    return () => { active = false; window.clearInterval(timer); };
  }, []);

  const monitoredGroups = useMemo(() => data.groups.filter(group => group.monitored), [data.groups]);
  const historicalGroups = useMemo(
    () => data.groups.filter(group => !group.monitored && (group.candidates > 0 || group.rejected > 0)),
    [data.groups],
  );
  const viewingHistory = groupView === 'history' || (monitoredGroups.length === 0 && historicalGroups.length > 0);
  const navigableGroups = viewingHistory ? historicalGroups : monitoredGroups;
  const activeChatId = navigableGroups.some(group => group.chat_id === selectedChatId)
    ? selectedChatId
    : navigableGroups[0]?.chat_id || '';
  const groupCandidates = useMemo(
    () => data.candidates.filter(candidate => !activeChatId || candidate.chat_id === activeChatId),
    [data.candidates, activeChatId],
  );
  const groupRejected = useMemo(
    () => data.rejected_candidates.filter(candidate => !activeChatId || candidate.chat_id === activeChatId),
    [data.rejected_candidates, activeChatId],
  );
  const activeGroup = data.groups.find(group => group.chat_id === activeChatId);

  const counts = useMemo(() => Object.fromEntries(filters.map(name => [name, (name === '已拒绝' ? groupRejected : groupCandidates).filter(candidate => {
    if (name === '全部') return true;
    if (name === '已拒绝') return true;
    if (name === '待确认') return candidate.filter.status === 'review';
    return candidate.score?.recommendation === name;
  }).length])), [groupCandidates, groupRejected]);
  const visible = useMemo(() => (filter === '已拒绝' ? groupRejected : groupCandidates).filter(candidate => filter === '全部' || filter === '已拒绝' || (filter === '待确认' ? candidate.filter.status === 'review' : candidate.score?.recommendation === filter)).sort((a, b) => (b.score?.total_score ?? -1) - (a.score?.total_score ?? -1) || b.filter.priority - a.filter.priority), [groupCandidates, groupRejected, filter]);
  const hasMonitoredGroups = monitoredGroups.length > 0;
  const isRunning = hasMonitoredGroups && data.health.status === 'running' && !data.health.consecutive_errors;
  const readCount = activeGroup?.messages ?? data.live_summary.messages;
  const orderCount = activeGroup?.orders ?? data.live_summary.orders;
  const monitorMessage = apiError
    ? '雷达后台未连接。请从系统托盘彻底退出家教雷达，再重新打开。'
    : !hasMonitoredGroups
      ? firstRun === 'verify' ? '已保存群聊，正在等待雷达开始读取。' : '登录电脑版微信后，打开「管理群聊」选择要监控的群。'
      : !isRunning
        ? '雷达暂未正常读群。请确认电脑版微信已登录，并检查托盘里是否暂停了监控。'
        : readCount === 0
          ? '正在监控，当前还没有读到可处理的新消息。等群里有新消息后再看这里。'
          : `已读取 ${readCount} 条群消息，解析出 ${orderCount} 条订单。${groupCandidates.length ? '' : ' 暂时没有符合条件的候选。'}`;
  const openMap = () => { if (filter === '已拒绝') setFilter('全部'); setView('map'); };
  const selectGroup = (chatId: string, targetView: 'active' | 'history') => {
    setGroupView(targetView);
    setSelectedChatId(chatId);
    setFilter('全部');
  };

  return (
    <main className="shell">
      <header className="topbar">
        <div className="brand"><span className="brand-mark" aria-hidden="true"><i /><i /><i /></span><div><p>本地候选面板</p><h1>家教雷达</h1></div></div>
        <div className="top-actions"><RadarSetup onFirstSave={() => setFirstRun('choose')} /><GroupManager firstRun={firstRun === 'choose'} onSaved={() => setFirstRun(current => current === 'choose' ? 'verify' : current)} /><details className="radar-tools"><summary className="manage-groups-button">工具</summary><div><LocalModelSetup /><MapBetaImport /></div></details><div className={`live-pill ${isRunning ? '' : 'offline'}`}><span />{isRunning ? '监控运行中' : apiError ? '数据服务未连接' : !hasMonitoredGroups ? '尚未选择群聊' : '监控状态异常'}</div></div>
      </header>
      {(firstRun === 'verify' || !groupCandidates.length || !isRunning) && <div className="notice" role="status">
        {firstRun === 'verify' && <><strong>首次使用 · 第 3 步，共 3 步</strong>{'　'}</>}{monitorMessage}
        {firstRun === 'verify' && isRunning && readCount > 0 && <>{'　'}<button className="manage-groups-button" onClick={() => setFirstRun(null)}>完成向导</button></>}
      </div>}
      {(activeGroup?.information_missing ?? data.information_missing_count) > 0 && <div className="notice">当前群候选中有 {activeGroup?.information_missing ?? data.information_missing_count} 条订单仍有字段待确认；它们会继续保留，并按缺失数据规则限制评分。</div>}
      {(monitoredGroups.length > 0 || historicalGroups.length > 0) && <>
        <nav className="group-tabs" aria-label="监控群切换">
          {monitoredGroups.map(group => <button key={group.chat_id} className={!viewingHistory && group.chat_id === activeChatId ? 'active' : ''} onClick={() => selectGroup(group.chat_id, 'active')}><span>{group.chat_name}</span><b>{group.candidates}</b><i className={group.status} /></button>)}
          {historicalGroups.length > 0 && <button className={`history-groups-entry ${viewingHistory ? 'active' : ''}`} onClick={() => selectGroup(historicalGroups[0].chat_id, 'history')}><span>历史群聊</span><b>{historicalGroups.reduce((total, group) => total + group.candidates, 0)}</b><i className="stopped" /></button>}
        </nav>
        {viewingHistory && historicalGroups.length > 0 && <nav className="history-group-tabs" aria-label="历史群聊切换">
          <span>已停止监控</span>
          {historicalGroups.map(group => <button key={group.chat_id} className={group.chat_id === activeChatId ? 'active' : ''} onClick={() => selectGroup(group.chat_id, 'history')}><span>{group.chat_name}</span><b>{group.candidates}</b></button>)}
        </nav>}
      </>}
      <section className="radar-heading">
        <div><p className="eyebrow">实时雷达</p><h2>{activeGroup?.chat_name || '家教订单'}</h2></div>
        <span>最近检查 · {checkedText(data.health.checked_at)}</span>
      </section>
      <section className="stats" aria-label="监控数据">
        <article><p>本次监听</p><strong>{activeGroup?.messages ?? data.live_summary.messages}</strong><span>消息 · 当前群</span></article>
        <article><p>本次解析</p><strong>{activeGroup?.orders ?? data.live_summary.orders}</strong><span>订单 · 当前群</span></article>
        <article className="stat-accent"><p>可看候选</p><strong>{groupCandidates.length}</strong><span>当前群独立保留</span></article>
        <article><p>本次淘汰</p><strong>{activeGroup?.orders_rejected ?? data.live_summary.rejected}</strong><span>当前群</span></article>
      </section>
      <section className="content-grid">
        <aside className="filter-panel">
          <p className="section-label">筛选视图</p>
          <nav aria-label="候选筛选">{filters.map(name => <button key={name} className={filter === name ? 'active' : ''} onClick={() => setFilter(name)}><span>{name === '全部' ? '全部候选' : name}</span><b>{counts[name]}</b></button>)}</nav>
          <div className="rule-note"><p>当前规则</p><strong>按个人条件筛选</strong><span>地点不按行政区限制，只按实际公交通勤评分</span></div>
        </aside>
        <section className="results">
          <div className="results-head"><div><p className="section-label">候选队列</p><h3>{view === 'map' ? '候选地图' : filter === '全部' ? '全部候选' : filter}</h3></div><div className="results-tools"><div className="view-toggle" aria-label="结果视图"><button className={view === 'list' ? 'active' : ''} onClick={() => setView('list')}>列表</button><button className={view === 'map' ? 'active' : ''} onClick={openMap}>地图</button></div><span className="source-tag">实时 + 回放 + 已保存 · {visible.length} 条</span></div></div>
          {view === 'map' && mapImportResult && <p role="status">{mapImportResult}</p>}
          {view === 'map' ? <CandidateMap candidates={visible} /> : <div className="cards">
            {visible.map((candidate, index) => (
              <article className="candidate" key={candidate.candidate_key || `${candidate.source}-${candidate.order.order_id}`}>
                <div className="rank">{String(index + 1).padStart(2, '0')}</div>
                <div className="candidate-main">
                  <div className="candidate-title"><div><span className="district">{sourceText(candidate.source)} · {candidate.order.district || '区域待确认'}</span><h4>{candidate.order.subjects?.join(' · ') || candidate.order.subject_text}</h4></div><div className={`score grade-${candidate.score?.recommendation || 'pending'}`}><b>{candidate.score?.recommendation || '—'}</b><strong>{candidate.score?.total_score ?? '待评分'}</strong><span>{candidate.score ? '综合分' : '等待完整数据'}</span></div></div>
                  <div className="meta-row"><span>{candidate.order.student_text || '学员信息待确认'}</span><span>{formatPay(candidate)}</span><span>{commuteText(candidate)}</span><span>{candidate.order.schedule_text || '时间待确认'}</span></div>
                  <p className="address">{candidate.order.address || '地址待确认'}</p>
                  {candidate.score && <div className="score-breakdown"><span>能力 <b>{candidate.score.component_scores.ability.score}/35</b></span><span>收益 <b>{candidate.score.component_scores.earnings.score}/30</b></span><span>通勤 <b>{candidate.score.component_scores.commute.score}/25</b></span><span>长期 <b>{candidate.score.component_scores.long_term.score}/10</b></span>{candidate.score.economics.effective_hourly_min !== null && <span className="effective-pay">真实时薪 <b>{candidate.score.economics.effective_hourly_min} 元</b></span>}</div>}
                  <div className="decision"><span>{candidate.filter.status === 'matched' ? '规则匹配' : '待确认'}</span>{candidate.filter.reasons.at(-1)}</div>
                  <div className="candidate-actions"><details><summary>查看评分依据、分析与原文</summary><div className="detail-body">{candidate.score && <><p><b>推荐原因</b>{candidate.score.recommendation_reasons.join('；') || '无'}</p><p><b>不推荐原因</b>{candidate.score.non_recommendation_reasons.join('；') || '无'}</p>{candidate.score.grade_caps.length > 0 && <p><b>等级限制</b>{candidate.score.grade_caps.map(item => `${item.grade}：${item.reason}`).join('；')}</p>}</>}{candidate.model_analysis?.status === 'complete' && <><p><b>模型说明</b>{candidate.model_analysis.summary}</p><p><b>补充匹配点</b>{candidate.model_analysis.fit_reasons?.join('；') || '无'}</p><p><b>补充风险</b>{candidate.model_analysis.risks?.join('；') || '无'}</p><p><b>待确认问题</b>{candidate.model_analysis.questions?.join('；') || '无'}</p></>}{candidate.recognition?.status === 'complete' && <p><b>识别补全</b>已根据原文补全 {candidate.recognition.accepted_fields?.length || 0} 个字段</p>}<p><b>筛选依据</b>{candidate.filter.reasons.join('；')}</p><p><b>老师要求</b>{candidate.order.requirements_text || '未填写'}</p>{candidate.order.current_level && <p><b>学生程度</b>{candidate.order.current_level}</p>}<section className="raw-order"><b>订单原文</b><pre>{candidate.order.raw_text || '该历史记录未保留原文'}</pre></section></div></details><button className={filter === '已拒绝' ? 'restore-button' : 'reject-button'} onClick={() => mutateCandidate(candidate, filter === '已拒绝' ? 'restore' : 'reject')}>{filter === '已拒绝' ? '恢复候选' : '不合适，移到已拒绝'}</button></div>
                </div>
                <div className="order-id">{candidate.order.order_id}</div>
              </article>
            ))}
            {!visible.length && <div className="empty"><strong>这一栏暂时没有候选</strong><span>有符合规则的新单时会自动出现在这里。</span></div>}
          </div>}
        </section>
      </section>
    </main>
  );
}
