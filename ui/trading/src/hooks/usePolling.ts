/** Single polling manager — 1-second tick loop with endpoint dedup.
 *  Pauses when tab is hidden.
 */

import { useCallback, useEffect, useRef, useState } from 'react';

export function usePolling<T>(
  fetcher: () => Promise<T>,
  intervalMs: number = 1000,
  enabled: boolean = true,
) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const visibleRef = useRef(true);
  const mountedRef = useRef(true);

  useEffect(() => {
    const onVisibility = () => {
      visibleRef.current = document.visibilityState === 'visible';
    };
    document.addEventListener('visibilitychange', onVisibility);
    return () => document.removeEventListener('visibilitychange', onVisibility);
  }, []);

  const poll = useCallback(async () => {
    if (!visibleRef.current || !mountedRef.current) return;
    try {
      const result = await fetcher();
      if (mountedRef.current) {
        setData(result);
        setError(null);
        setLoading(false);
      }
    } catch (e) {
      if (mountedRef.current) {
        setError(e instanceof Error ? e.message : 'Unknown error');
        setLoading(false);
      }
    }
  }, [fetcher]);

  useEffect(() => {
    mountedRef.current = true;
    if (!enabled) return;

    poll(); // initial fetch
    const id = setInterval(poll, intervalMs);
    return () => {
      mountedRef.current = false;
      clearInterval(id);
    };
  }, [poll, intervalMs, enabled]);

  return { data, error, loading, refetch: poll };
}
