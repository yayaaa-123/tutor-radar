'use client';

import { useEffect, useMemo, useRef, useState } from 'react';
import { createMapScriptLoader } from './mapScriptLoader';
import { formatMapFrequency } from './mapFrequency';

type LocationPoint = {
  location?: string;
};

export type MapCandidate = {
  candidate_key?: string;
  order: {
    order_id: string;
    address: string;
    subjects: string[];
    subject_text: string;
    pay_text: string;
    hourly_rate_min: number | null;
    hourly_rate_max: number | null;
    frequency_per_week?: number | null;
    schedule_text?: string | null;
  };
  commute?: {
    status: string;
    origin?: LocationPoint;
    destination?: LocationPoint;
    route?: { duration_minutes?: number };
  } | null;
  score?: { recommendation: 'S' | 'A' | 'B' | 'C' } | null;
};

type MapClientConfig = {
  configured: boolean;
  js_key: string | null;
  security_code: string | null;
  style_id: string | null;
  message: string | null;
};

type RouteSegment = {
  mode: 'walking' | 'transit';
  name: string;
  path: [number, number][];
};

type RouteResponse = {
  status: string;
  route?: { duration_minutes?: number; segments?: RouteSegment[] };
  error?: string;
};

type MapOverlay = {
  on?: (event: string, handler: () => void) => void;
};

type MapInstance = {
  on?: (event: string, handler: () => void) => void;
  remove: (overlays: MapOverlay[]) => void;
  setFitView: (overlays?: MapOverlay[], immediately?: boolean, padding?: number[], maxZoom?: number) => void;
  destroy: () => void;
};

type InfoWindowInstance = {
  setContent: (content: HTMLElement) => void;
  open: (map: MapInstance, position: [number, number]) => void;
};

type AMapNamespace = {
  Map: new (container: HTMLElement, options: Record<string, unknown>) => MapInstance;
  Marker: new (options: Record<string, unknown>) => MapOverlay;
  Polyline: new (options: Record<string, unknown>) => MapOverlay;
  InfoWindow: new (options: Record<string, unknown>) => InfoWindowInstance;
};

declare global {
  interface Window {
    AMap?: AMapNamespace;
    _AMapSecurityConfig?: { securityJsCode: string };
    forbidenWebGL?: boolean;
  }
}

const loadMapScript = createMapScriptLoader<AMapNamespace>({
  getSDK: () => {
    const sdk = window.AMap;
    return sdk?.Map && sdk.Marker && sdk.InfoWindow && sdk.Polyline ? sdk : undefined;
  },
});
type MapRenderer = 'vector' | 'raster';
const MAP_RENDERER_STORAGE_KEY = 'tutor-radar-map-renderer';

function supportsWebGL(): boolean {
  try {
    const canvas = document.createElement('canvas');
    return Boolean(canvas.getContext('webgl2') || canvas.getContext('webgl'));
  } catch {
    return false;
  }
}

function loadAmap(jsKey: string, securityCode: string, renderer: MapRenderer): Promise<AMapNamespace> {
  window._AMapSecurityConfig = { securityJsCode: securityCode };
  if (renderer === 'raster') window.forbidenWebGL = true;
  else delete window.forbidenWebGL;
  return loadMapScript(`https://webapi.amap.com/maps?v=2.0&key=${encodeURIComponent(jsKey)}`);
}

function parseLocation(value?: string): [number, number] | null {
  if (!value) return null;
  const [longitude, latitude] = value.split(',').map(Number);
  return Number.isFinite(longitude) && Number.isFinite(latitude) ? [longitude, latitude] : null;
}

function formatPay(candidate: MapCandidate): string {
  if (candidate.order.pay_text) return candidate.order.pay_text;
  if (candidate.order.hourly_rate_min !== null) {
    const high = candidate.order.hourly_rate_max;
    return high && high !== candidate.order.hourly_rate_min
      ? `${candidate.order.hourly_rate_min}-${high} 元/小时`
      : `${candidate.order.hourly_rate_min} 元/小时`;
  }
  return '工资待确认';
}

const GRADE_MARKER_COLORS: Record<string, string> = {
  S: '#0d7b49',
  A: '#78982f',
  B: '#c98235',
  C: '#b64a3f',
  pending: '#7d8781',
};

function candidateMarkerColor(candidate: MapCandidate): string {
  return GRADE_MARKER_COLORS[candidate.score?.recommendation || 'pending'];
}

function createMarkerContent(label: string, color: string, origin = false): HTMLElement {
  const wrapper = document.createElement('div');
  wrapper.className = origin ? 'map-marker map-marker-origin' : 'map-marker';
  wrapper.setAttribute('aria-label', label);
  wrapper.title = label;
  const dot = document.createElement('span');
  dot.style.backgroundColor = color;
  wrapper.appendChild(dot);
  return wrapper;
}

function createPopup(candidate: MapCandidate, routeState: string): HTMLElement {
  const root = document.createElement('div');
  root.className = 'map-popup';
  const subject = document.createElement('strong');
  subject.textContent = candidate.order.subjects?.join(' · ') || candidate.order.subject_text || '科目待确认';
  const details = [
    ['工资', formatPay(candidate)],
    ['频率', formatMapFrequency(candidate.order)],
    ['详细地址', candidate.order.address || '地址待确认'],
    ['通勤', candidate.commute?.route?.duration_minutes ? `约 ${candidate.commute.route.duration_minutes} 分钟` : '待查询'],
  ];
  root.appendChild(subject);
  for (const [label, value] of details) {
    const row = document.createElement('p');
    const name = document.createElement('b');
    name.textContent = label;
    const content = document.createElement('span');
    content.textContent = value;
    row.appendChild(name);
    row.appendChild(content);
    root.appendChild(row);
  }
  const route = document.createElement('em');
  route.textContent = routeState;
  root.appendChild(route);
  return root;
}

export default function CandidateMap({ candidates }: { candidates: MapCandidate[] }) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [status, setStatus] = useState<'loading' | 'ready' | 'missing' | 'error'>('loading');
  const [message, setMessage] = useState('正在准备地图…');
  const [renderer, setRenderer] = useState<MapRenderer>('vector');
  const [webglAvailable, setWebglAvailable] = useState(true);
  const [reloadVersion, setReloadVersion] = useState(0);
  const markerPayload = useMemo(() => candidates.flatMap(candidate => {
    const destination = parseLocation(candidate.commute?.destination?.location);
    return destination ? [{ candidate, destination }] : [];
  }), [candidates]);
  const markerSignature = JSON.stringify(markerPayload);

  useEffect(() => {
    let disposed = false;
    let map: MapInstance | null = null;
    let mapReadyTimer: ReturnType<typeof setTimeout> | undefined;
    const configController = new AbortController();
    const configTimer = setTimeout(() => configController.abort(), 8000);
    const routeOverlays: MapOverlay[] = [];
    const blockedResources = new Set<string>();
    const markerData = JSON.parse(markerSignature) as { candidate: MapCandidate; destination: [number, number] }[];

    const handlePolicyViolation = (event: SecurityPolicyViolationEvent) => {
      let blocked = event.blockedURI || 'unknown';
      try {
        blocked = new URL(blocked).origin;
      } catch {
        blocked = blocked.includes(':') ? `${blocked.split(':', 1)[0]}:` : blocked;
      }
      blockedResources.add(`${event.effectiveDirective} ${blocked}`);
      if (!disposed) {
        setStatus('error');
        setMessage(`地图资源被桌面安全策略拦截：${[...blockedResources].join('；')}`);
      }
    };
    document.addEventListener('securitypolicyviolation', handlePolicyViolation);

    const initialize = async () => {
      try {
        setStatus('loading');
        const webglSupported = supportsWebGL();
        setWebglAvailable(webglSupported);
        const savedRenderer = localStorage.getItem(MAP_RENDERER_STORAGE_KEY);
        const requestedRenderer: MapRenderer = savedRenderer === 'raster' ? 'raster' : 'vector';
        const activeRenderer: MapRenderer = requestedRenderer === 'vector' && !webglSupported ? 'raster' : requestedRenderer;
        setRenderer(activeRenderer);
        const configResponse = await fetch('http://localhost:18765/api/map/config', {
          cache: 'no-store', signal: configController.signal,
        });
        if (!configResponse.ok) throw new Error('无法读取本地地图配置，请确认后台服务已启动');
        const config = await configResponse.json() as MapClientConfig;
        clearTimeout(configTimer);
        if (disposed) return;
        if (!config.configured || !config.js_key || !config.security_code) {
          if (!disposed) {
            setStatus('missing');
            setMessage(config.message || '地图底图尚未配置');
          }
          return;
        }
        if (!containerRef.current) return;
        setMessage('正在连接高德地图，连接失败时最多尝试 3 次…');
        const AMap = await loadAmap(config.js_key, config.security_code, activeRenderer);
        if (disposed || !containerRef.current) return;
        const mapOptions: Record<string, unknown> = {
          zoom: 10,
          viewMode: '2D',
        };
        if (activeRenderer === 'vector') {
          const customStyleId = config.style_id && /^[0-9a-f]{32}$/i.test(config.style_id) ? config.style_id : null;
          mapOptions.mapStyle = `amap://styles/${customStyleId || 'whitesmoke'}`;
          if (!customStyleId) mapOptions.features = ['bg', 'road'];
          mapOptions.showBuildingBlock = false;
        }
        map = new AMap.Map(containerRef.current, mapOptions);
        setMessage('正在加载地图底图…');
        mapReadyTimer = setTimeout(() => {
          if (disposed || blockedResources.size) return;
          setStatus('error');
          setMessage('地图底图加载超时，请检查网络后点击“重新加载地图”。候选列表和后台监控不受影响。');
        }, 20000);
        map.on?.('complete', () => {
          clearTimeout(mapReadyTimer);
          if (disposed || blockedResources.size) return;
          setStatus('ready');
          const rendererLabel = activeRenderer === 'vector'
            ? (config.style_id ? '自定义矢量地图' : '精简矢量地图')
            : '兼容地图';
          setMessage(markerData.length ? `${rendererLabel} · 已标记 ${markerData.length} 个候选` : `${rendererLabel} · 当前候选还没有可用坐标`);
        });
        const infoWindow = new AMap.InfoWindow({ offset: [0, -24], closeWhenClickMap: true });
        const markers: MapOverlay[] = [];
        const origin = markerData.map(item => parseLocation(item.candidate.commute?.origin?.location)).find(Boolean) || null;
        if (origin) {
          const originMarker = new AMap.Marker({
            position: origin,
            anchor: 'center',
            content: createMarkerContent('通勤出发地', '#146c43', true),
            map,
          });
          markers.push(originMarker);
        }
        for (const { candidate, destination } of markerData) {
          const label = candidate.order.subjects?.join('·') || candidate.order.subject_text || '候选';
          const marker = new AMap.Marker({
            position: destination,
            anchor: 'center',
            content: createMarkerContent(`${candidate.score?.recommendation || '待评分'} · ${label} · ${candidate.order.address || '地址待确认'}`, candidateMarkerColor(candidate)),
            map,
          });
          marker.on?.('click', () => {
            if (!map) return;
            if (routeOverlays.length) {
              map.remove(routeOverlays.splice(0, routeOverlays.length));
            }
            infoWindow.setContent(createPopup(candidate, '正在加载通勤轨迹…'));
            infoWindow.open(map, destination);
            const candidateKey = encodeURIComponent(candidate.candidate_key || candidate.order.order_id);
            fetch(`http://localhost:18765/api/route?candidate_key=${candidateKey}`, { cache: 'no-store' })
              .then(response => response.ok ? response.json() as Promise<RouteResponse> : Promise.reject(new Error('route unavailable')))
              .then(result => {
                if (disposed || !map || result.status !== 'ok' || !result.route?.segments?.length) throw new Error('route unavailable');
                for (const segment of result.route.segments) {
                  const overlay = new AMap.Polyline({
                    path: segment.path,
                    strokeColor: segment.mode === 'walking' ? '#8a928d' : '#146c43',
                    strokeWeight: segment.mode === 'walking' ? 5 : 7,
                    strokeOpacity: segment.mode === 'walking' ? 0.72 : 0.9,
                    strokeStyle: segment.mode === 'walking' ? 'dashed' : 'solid',
                    lineJoin: 'round',
                    lineCap: 'round',
                    showDir: segment.mode === 'transit',
                    map,
                  });
                  routeOverlays.push(overlay);
                }
                infoWindow.setContent(createPopup(candidate, `轨迹已加载 · ${result.route?.duration_minutes || candidate.commute?.route?.duration_minutes || '约'} 分钟`));
                infoWindow.open(map, destination);
                map.setFitView([...routeOverlays, marker], false, [70, 70, 70, 70], 13);
              })
              .catch(() => {
                if (!disposed && map) {
                  infoWindow.setContent(createPopup(candidate, '轨迹暂时加载失败，通勤时间仍可参考'));
                  infoWindow.open(map, destination);
                }
              });
          });
          markers.push(marker);
        }
        if (markers.length) map.setFitView(markers, false, [64, 64, 64, 64], 12);
      } catch (error) {
        clearTimeout(mapReadyTimer);
        map?.destroy();
        map = null;
        if (!disposed && !blockedResources.size) {
          setStatus('error');
          setMessage(configController.signal.aborted
            ? '本地地图配置读取超时，请确认后台服务已启动后重新加载地图。'
            : error instanceof Error && error.message.startsWith('高德')
              ? error.message
              : '地图加载失败，请重新加载地图。候选列表和后台监控不受影响。');
        }
      } finally {
        clearTimeout(configTimer);
      }
    };

    initialize();
    return () => {
      disposed = true;
      clearTimeout(configTimer);
      clearTimeout(mapReadyTimer);
      configController.abort();
      document.removeEventListener('securitypolicyviolation', handlePolicyViolation);
      map?.destroy();
    };
  }, [markerSignature, reloadVersion]);

  const switchRenderer = () => {
    const nextRenderer: MapRenderer = renderer === 'vector' ? 'raster' : 'vector';
    localStorage.setItem(MAP_RENDERER_STORAGE_KEY, nextRenderer);
    window.location.reload();
  };

  return (
    <div className="candidate-map-shell">
      <div className="map-status">
        <span className={status} /><output aria-live="polite">{message}</output>
        <button type="button" disabled={status === 'loading'} onClick={() => {
          setStatus('loading');
          setMessage('正在重新加载地图…');
          setReloadVersion(version => version + 1);
        }}>{status === 'loading' ? '加载中…' : '重新加载地图'}</button>
        {webglAvailable && <button type="button" onClick={switchRenderer}>{renderer === 'vector' ? '使用兼容地图' : '尝试精简地图'}</button>}
      </div>
      <div className={`candidate-map map-renderer-${renderer}`} ref={containerRef} aria-label="候选家教地图" />
      {(status === 'missing' || status === 'error') && (
        <div className="map-placeholder">
          <strong>{status === 'missing' ? '还差一步地图配置' : '地图暂时不可用'}</strong>
          <span>{message}</span>
          {status === 'missing' && <small>后台通勤查询仍正常；配置高德 JS API Key 和安全密钥后即可显示地图。</small>}
        </div>
      )}
      <div className="map-legend"><span><i className="origin" />出发点</span><span><i className="grade-S" />S</span><span><i className="grade-A" />A</span><span><i className="grade-B" />B</span><span><i className="grade-C" />C</span><span><i className="walk" />步行</span><span><i className="transit" />公交 / 地铁</span></div>
    </div>
  );
}
