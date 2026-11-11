// 실효 설정 규칙(내부 API 와 설정 페이지 공용) 단위 테스트.
import { describe, expect, it } from "vitest";
import { CatalogMismatch, CorruptPreference, effectivePreference } from "../src/preferences-model";

const order = ["fsc", "fss", "better", "pipc", "assembly"];

describe("effectivePreference", () => {
  it("행 없음·all 은 전체 수신", () => {
    expect(effectivePreference(null, null, order)).toEqual({ mode: "all", agencies: [] });
    expect(effectivePreference("all", "[]", order)).toEqual({ mode: "all", agencies: [] });
  });
  it("selected 는 카탈로그 순서로", () => {
    expect(effectivePreference("selected", '["assembly","fsc"]', order)).toEqual({
      mode: "selected",
      agencies: ["fsc", "assembly"],
    });
  });
  it("카탈로그에 없는 기관이 하나라도 있으면 CatalogMismatch(좁히지도 넓히지도 않음)", () => {
    expect(() => effectivePreference("selected", '["assembly","fsc","gone"]', order)).toThrow(CatalogMismatch);
  });
  it("빈 선택은 일시중지로 유지", () => {
    expect(effectivePreference("selected", "[]", order)).toEqual({ mode: "selected", agencies: [] });
  });
  it("선택이 카탈로그 변경으로 모두 사라져도 전체 수신으로 확대하지 않고 CatalogMismatch", () => {
    expect(() => effectivePreference("selected", '["gone"]', order)).toThrow(CatalogMismatch);
  });
  it("손상된 값은 추측하지 않고 예외", () => {
    expect(() => effectivePreference("bogus", "[]", order)).toThrow(CorruptPreference);
    expect(() => effectivePreference("selected", "{bad", order)).toThrow(CorruptPreference);
    expect(() => effectivePreference("selected", "[1]", order)).toThrow(CorruptPreference);
    expect(() => effectivePreference("selected", null, order)).toThrow(CorruptPreference);
  });
});
