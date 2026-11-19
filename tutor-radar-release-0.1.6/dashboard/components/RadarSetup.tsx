import { useEffect, useRef, useState } from 'react';

const subjects = ['语文', '数学', '英语', '物理', '化学', '生物', '历史', '地理', '政治'];
const endpoint = 'http://localhost:18765/api/setup';
type Profile = {
  gender?: string; education?: string; school?: string; year?: string;
  origin?: string; subjects?: string[]; weekend_only?: boolean;
};

export default function RadarSetup({ onFirstSave }: { onFirstSave: () => void }) {
  const [profile, setProfile] = useState<Profile | null>(null);
  const dialog = useRef<HTMLDialogElement>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');

  useEffect(() => {
    let stopped = false;
    async function check() {
      try {
        const response = await fetch(endpoint);
        if (response.ok && !stopped) {
          const data = await response.json();
          setProfile(data.profile);
          if (!data.configured && !dialog.current?.open) dialog.current?.showModal();
          window.clearInterval(timer);
        }
      } catch {
        // The local service may still be starting.
      }
    }
    void check();
    const timer = window.setInterval(check, 5000);
    return () => { stopped = true; window.clearInterval(timer); };
  }, []);

  return <>
    <button className="manage-groups-button" onClick={() => {
      setMessage('');
      dialog.current?.showModal();
    }}>我的条件</button>
    <dialog ref={dialog} className="profile-dialog" aria-labelledby="profile-title">
    <header><div><p className="section-label">{profile ? '实时雷达' : '首次使用 · 第 1 步，共 3 步'}</p><h2 id="profile-title">{profile ? '我的条件' : '设置个人条件'}</h2></div>
      <button type="button" className="manager-close" aria-label="关闭设置" onClick={() => dialog.current?.close()}>×</button>
    </header>
    <p className="profile-description">{profile ? '这些条件用于筛选新收到的家教订单。' : '先登录电脑版微信。填好个人条件后，雷达会带你选择要监控的群。'}</p>
    <form key={JSON.stringify(profile)} onSubmit={async event => {
      event.preventDefault();
      const data = new FormData(event.currentTarget);
      setBusy(true);
      setMessage('');
      try {
        const values = {
          gender: String(data.get('gender')), education: String(data.get('education')),
          school: String(data.get('school')), year: String(data.get('year')), origin: String(data.get('origin')),
          db_dir: String(data.get('db_dir') || ''), subjects: data.getAll('subjects').map(String),
          weekend_only: data.has('weekend_only'),
        };
        const response = await fetch(endpoint, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(values),
        });
        if (!response.ok) throw new Error();
        setMessage(profile ? '已保存，下次检查消息时生效。' : '');
        setProfile(values);
        dialog.current?.close();
        if (!profile) onFirstSave();
      } catch {
        setMessage('保存失败，请至少选择一门学科，并检查本地服务是否可用。');
      } finally {
        setBusy(false);
      }
    }}>
      <div className="profile-grid">
        <label>性别<select name="gender" required defaultValue={profile?.gender || ''}><option value="">请选择</option><option value="male">男</option><option value="female">女</option></select></label>
        <label>学历<select name="education" required defaultValue={profile?.education || ''}><option value="">请选择</option>{Array.from(new Set(['大专', '本科', '硕士', '博士', '其他', ...(profile?.education ? [profile.education] : [])])).map(value => <option key={value}>{value}</option>)}</select></label>
        <label>学校（选填）<input name="school" maxLength={100} defaultValue={profile?.school} /></label>
        <label>年级（选填）<input name="year" maxLength={30} defaultValue={profile?.year} /></label>
      </div>
      <fieldset><legend>可教学科</legend><div className="subject-grid">{subjects.map(subject => <label key={subject}><input name="subjects" type="checkbox" value={subject} defaultChecked={profile?.subjects?.includes(subject)} /><span>{subject}</span></label>)}</div></fieldset>
      <label className="profile-weekend"><input name="weekend_only" type="checkbox" defaultChecked={profile?.weekend_only} /> 只接周末</label>
      <label className="profile-field">通勤出发地<input name="origin" placeholder="深圳的地铁站或地标" required maxLength={120} defaultValue={profile?.origin} /></label>
      {!profile && <details><summary>高级设置</summary><label className="profile-field">微信数据目录（可留空自动查找）<input name="db_dir" maxLength={512} /></label></details>}
      {message && <p role="status">{message}</p>}
      <footer><button type="button" className="manager-cancel" onClick={() => dialog.current?.close()}>取消</button><button className="manager-save" disabled={busy}>{profile ? '保存条件' : '保存并选群'}</button></footer>
    </form>
    </dialog>
    {message && !busy && <span className="profile-feedback" role="status">{message}</span>}
  </>;
}
