import { useEffect, useState } from "react";

type Health = { status: string; version: string };

export default function App() {
  const [health, setHealth] = useState<Health | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetch("/api/health")
      .then((res) => (res.ok ? res.json() : Promise.reject(new Error(`HTTP ${res.status}`))))
      .then(setHealth)
      .catch((err: Error) => setError(err.message));
  }, []);

  return (
    <main>
      <h1>Sparring Partner</h1>
      <p>A debugging coach that knows the answer but won't tell you.</p>
      <p className="status">
        Backend:{" "}
        {health ? `${health.status} (v${health.version})` : error ? `unreachable (${error})` : "checking…"}
      </p>
    </main>
  );
}
