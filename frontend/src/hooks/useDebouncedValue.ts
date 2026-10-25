import { useEffect, useState } from 'react';

/** Wait for a short typing pause before starting an expensive server search. */
export function useDebouncedValue<T>(value: T, delay = 250): T {
  const [settled, setSettled] = useState<T>(() => value);
  useEffect(() => {
    const timer = window.setTimeout(() => setSettled(() => value), delay);
    return () => window.clearTimeout(timer);
  }, [value, delay]);
  return settled;
}
