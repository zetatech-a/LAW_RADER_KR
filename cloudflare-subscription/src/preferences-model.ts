// 저장된 설정 → 실효 설정 변환. 내부 API(Python 응답)와 설정 페이지가 같은 규칙을 쓴다
// (사용자가 화면에서 보는 상태 = 다음 발송에 실제로 적용되는 상태).

export type Mode = "all" | "selected";

export interface Effective {
  mode: Mode;
  agencies: string[];
}

export class CorruptPreference extends Error {}

/** 저장된 선택 기관이 현재 카탈로그에 없다 — 운영 설정 오류. 선택을 추측해 넓히거나 좁히지 않는다. */
export class CatalogMismatch extends Error {}

/**
 * - 행 없음 / mode=all → 전체 수신
 * - selected → 저장된 기관을 카탈로그 순서로. 저장값은 바꾸지 않는다(기관을 되돌리면 그대로 복원).
 * - selected 인데 저장된 기관 중 하나라도 현재 카탈로그에 없음 → CatalogMismatch.
 *   전체 수신으로 확대하면 원하지 않은 기관의 메일이 가고, 조용히 좁히면 선택이 사라진다.
 * - selected + 빈 목록(사용자가 고른 일시중지)은 그대로 둔다
 * 저장값이 손상돼 있으면 예외 — 호출자는 추측하지 않고 실패로 응답한다.
 */
export function effectivePreference(
  mode: string | null,
  agenciesJson: string | null,
  catalogOrder: readonly string[],
): Effective {
  if (mode === null || mode === "all") return { mode: "all", agencies: [] };
  if (mode !== "selected") throw new CorruptPreference("mode");
  let stored: unknown;
  try {
    stored = JSON.parse(agenciesJson ?? "");
  } catch {
    throw new CorruptPreference("agencies");
  }
  if (!Array.isArray(stored) || !stored.every((a) => typeof a === "string")) {
    throw new CorruptPreference("agencies");
  }
  const known = new Set(catalogOrder);
  if ((stored as string[]).some((id) => !known.has(id))) throw new CatalogMismatch("unknown_agency");
  const chosen = new Set(stored as string[]);
  return { mode: "selected", agencies: catalogOrder.filter((id) => chosen.has(id)) };
}
