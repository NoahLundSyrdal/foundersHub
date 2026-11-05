export async function api<T>(path: string, body?: unknown): Promise<T> {
  const response = await fetch(
    `/api${path}`,
    body === undefined
      ? undefined
      : {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        },
  );
  let data: unknown;
  try {
    data = await response.json();
  } catch {
    throw new Error(`The Python API returned an unreadable response (${response.status}). Check that the backend is running.`);
  }
  if (!response.ok) {
    const payload = data && typeof data === "object" ? data as Record<string, unknown> : {};
    const detail = payload.error ?? payload.detail;
    const message = typeof detail === "string" ? detail : Array.isArray(detail)
      ? detail.map((entry) => entry && typeof entry === "object" && typeof entry.msg === "string" ? entry.msg : "Invalid request").join("; ")
      : `Request failed (${response.status})`;
    throw new Error(message);
  }
  return data as T;
}
