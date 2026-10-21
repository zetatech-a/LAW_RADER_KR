// 저장된 설정 → 실효 설정 변환. 내부 API(Python 응답)와 설정 페이지가 같은 규칙을 쓴다
// (사용자가 화면에서 보는 상태 = 다음 발송에 실제로 적용되는 상태).

export type Mode = "all" | "selected";

export interface Effective {
  mode: Mode;
  agencies: string[];
}

export class CorruptPreference extends Error {}

/**
 * - 행 없음 / mode=all → 전체 수신
 * - selected → 저장된 기관 중 현재 카탈로그에 있는 것만, 카탈로그 순서로
 * - selected 이고 저장 목록은 비어 있지 않았는데 카탈로그 변경으로 모두 사라짐
 *   → 조용한 수신 중단을 막기 위해 전체 수신으로 본다(README 계약)
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
  const chosen = new Set(stored as string[]);
  const kept = catalogOrder.filter((id) => chosen.has(id));
  if (stored.length > 0 && kept.length === 0) return { mode: "all", agencies: [] };
  return { mode: "selected", agencies: kept };
}
