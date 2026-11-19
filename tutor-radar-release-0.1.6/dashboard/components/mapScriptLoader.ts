export const MAP_SCRIPT_ATTEMPTS = 3;

type LoaderOptions<T> = {
  getSDK: () => T | undefined;
  createScript?: () => HTMLScriptElement;
  appendScript?: (script: HTMLScriptElement) => void;
  timeoutMs?: number;
  retryDelaysMs?: number[];
};

// Share only the active request. A rejected request must never poison later loads.
export function createMapScriptLoader<T>({
  getSDK,
  createScript = () => document.createElement('script'),
  appendScript = script => document.head.appendChild(script),
  timeoutMs = 12000,
  retryDelaysMs = [1000, 2000],
}: LoaderOptions<T>) {
  let pending: Promise<T> | null = null;

  function attempt(src: string): Promise<T> {
    return new Promise((resolve, reject) => {
      const script = createScript();
      let settled = false;
      const finish = (error?: Error) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        script.onload = null;
        script.onerror = null;
        const sdk = getSDK();
        if (error || !sdk) {
          script.remove();
          reject(error || new Error('高德地图脚本未能完成初始化'));
        } else {
          resolve(sdk);
        }
      };
      const timer = setTimeout(() => finish(new Error('高德地图脚本加载超时')), timeoutMs);
      script.src = src;
      script.async = true;
      script.onload = () => finish();
      script.onerror = () => finish(new Error('高德地图资源连接失败'));
      try {
        appendScript(script);
      } catch {
        finish(new Error('无法加载高德地图脚本'));
      }
    });
  }

  return function load(src: string): Promise<T> {
    const sdk = getSDK();
    if (sdk) return Promise.resolve(sdk);
    if (pending) return pending;
    pending = (async () => {
      for (let index = 0; index < MAP_SCRIPT_ATTEMPTS; index += 1) {
        if (index > 0) {
          await new Promise(resolve => setTimeout(resolve, retryDelaysMs[index - 1] ?? 2000));
          const lateSDK = getSDK();
          if (lateSDK) return lateSDK;
        }
        try {
          return await attempt(src);
        } catch (error) {
          if (index === MAP_SCRIPT_ATTEMPTS - 1) {
            throw new Error(`${error instanceof Error ? error.message : '地图加载失败'}，已尝试 ${MAP_SCRIPT_ATTEMPTS} 次。请检查网络后重新加载地图。`);
          }
        }
      }
      throw new Error('地图加载失败');
    })().finally(() => { pending = null; });
    return pending;
  };
}
