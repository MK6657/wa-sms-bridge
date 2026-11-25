/**
 * 工作台价格档位绑定与切换隔离逻辑（修复复核 P2-01）。
 *
 * 规则：
 * 1. 点击实时价档矩阵写入的价格严格绑定所属 (provider, countryId, service) 组合；
 * 2. 切换国家、服务或平台时立即清除不匹配的价档选择，杜绝旧组合价档随新下单提交；
 * 3. 用户在高级表单中手工输入的目标价（manualTargetPrice）与价档选择清晰分离，互不混淆；
 * 4. 异步价格快照回包同时校验请求序号与 (provider, countryId, service) 组合，彻底杜绝迟到旧包覆盖。
 */

export interface PriceCombo {
  provider: string;
  countryId: string;
  service: string;
}

export interface BoundTierSelection extends PriceCombo {
  cost: number;
}

export interface PriceSelectionState {
  selectedTier: BoundTierSelection | null;
  manualTargetPrice: string;
  priceLock: boolean;
}

export function sameCombo(a: PriceCombo | null | undefined, b: PriceCombo | null | undefined): boolean {
  if (!a || !b) return false;
  return (
    a.provider.trim().toLowerCase() === b.provider.trim().toLowerCase() &&
    String(a.countryId) === String(b.countryId) &&
    a.service.trim().toLowerCase() === b.service.trim().toLowerCase()
  );
}

export function selectPriceTier(
  state: PriceSelectionState,
  combo: PriceCombo,
  cost: number,
): PriceSelectionState {
  if (
    state.selectedTier &&
    sameCombo(state.selectedTier, combo) &&
    Math.abs(state.selectedTier.cost - cost) < 0.0002
  ) {
    const hasManual = Number(state.manualTargetPrice) > 0;
    return {
      ...state,
      selectedTier: null,
      priceLock: hasManual ? state.priceLock : false,
    };
  }
  return {
    ...state,
    selectedTier: { ...combo, cost },
    manualTargetPrice: "",
  };
}

export function setManualPrice(
  state: PriceSelectionState,
  value: string,
): PriceSelectionState {
  return {
    ...state,
    selectedTier: null,
    manualTargetPrice: value,
  };
}

export function reconcileComboChange(
  state: PriceSelectionState,
  newCombo: PriceCombo | null,
): PriceSelectionState {
  if (!state.selectedTier) {
    return state;
  }
  if (newCombo && sameCombo(state.selectedTier, newCombo)) {
    return state;
  }
  // 切换国家/服务/平台后，立即清掉旧组合绑定的价档；若未手工填价格且原仅依赖价档锁价，同时关闭锁价防误提交
  const hasManual = Number(state.manualTargetPrice) > 0;
  return {
    ...state,
    selectedTier: null,
    priceLock: hasManual ? state.priceLock : false,
  };
}

export function resolveEffectiveTargetPrice(
  state: PriceSelectionState,
  currentCombo: PriceCombo | null,
): { value: number | undefined; displayValue: string; source: "tier" | "manual" | "none" } {
  if (state.selectedTier && currentCombo && sameCombo(state.selectedTier, currentCombo)) {
    return {
      value: state.selectedTier.cost,
      displayValue: String(state.selectedTier.cost),
      source: "tier",
    };
  }
  const trimmed = state.manualTargetPrice.trim();
  if (trimmed) {
    const parsed = Number(trimmed);
    if (Number.isFinite(parsed) && parsed > 0) {
      return { value: parsed, displayValue: trimmed, source: "manual" };
    }
    return { value: undefined, displayValue: trimmed, source: "manual" };
  }
  return { value: undefined, displayValue: "", source: "none" };
}

export function shouldAcceptPriceSnapshot(
  reqSeq: number,
  latestSeq: number,
  snapshotCombo: PriceCombo,
  currentCombo: PriceCombo | null,
): boolean {
  if (reqSeq !== latestSeq) return false;
  if (!currentCombo) return false;
  return sameCombo(snapshotCombo, currentCombo);
}
