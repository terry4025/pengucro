from __future__ import annotations

from typing import Any

from engines.cgv_engine_visitor_runtime import CgvEngine as VisitorCgvEngine


class CgvEngine(VisitorCgvEngine):
    """DOM-tolerant final visitor runtime.

    CGV has changed wrapper elements around the '일반' visitor label before.
    Do not require that label to be a leaf node: find any visible element whose
    normalized text is exactly '일반', prefer the smallest wrapper, then search
    its ancestors for the requested numeric button.
    """

    @staticmethod
    def _visitor_ui_snapshot(page, people: int) -> dict[str, Any]:
        try:
            result = page.evaluate(
                r"""
                people => {
                  const clean = value => String(value || '').replace(/\s+/g, '');
                  const visible = node => {
                    if (!node) return false;
                    const style = window.getComputedStyle(node);
                    const rect = node.getBoundingClientRect();
                    return style.display !== 'none' && style.visibility !== 'hidden' &&
                           rect.width > 0 && rect.height > 0;
                  };
                  const selected = node => {
                    if (!node) return false;
                    const classes = String(node.className || '').toLowerCase();
                    const tokens = new Set(classes.split(/[\s_\-]+/));
                    return node.title === '선택됨' ||
                           node.getAttribute('aria-pressed') === 'true' ||
                           node.getAttribute('aria-selected') === 'true' ||
                           node.getAttribute('aria-checked') === 'true' ||
                           node.getAttribute('data-selected') === 'true' ||
                           tokens.has('selected') || tokens.has('active') || tokens.has('on');
                  };

                  const path = String(location.pathname || '');
                  const seatButtons = Array.from(
                    document.querySelectorAll('button[data-seatlocno]')
                  ).filter(visible);
                  const controls = Array.from(
                    document.querySelectorAll('button, a, div[role="button"]')
                  ).filter(visible);
                  const hasControl = label => controls.some(node => clean(node.textContent) === label);
                  const modalOpen = seatButtons.length > 0 ||
                                    hasControl('인원변경') || hasControl('선택완료');

                  const labels = Array.from(document.querySelectorAll('body *'))
                    .filter(node => visible(node) && clean(node.textContent) === '일반')
                    .sort((left, right) => {
                      const childGap = left.children.length - right.children.length;
                      if (childGap) return childGap;
                      return left.getBoundingClientRect().width - right.getBoundingClientRect().width;
                    });
                  let target = null;
                  for (const label of labels) {
                    let box = label;
                    for (let depth = 0; depth < 10 && box; depth += 1, box = box.parentElement) {
                      const candidate = Array.from(box.querySelectorAll('button')).find(button =>
                        visible(button) && clean(button.textContent) === String(people)
                      );
                      if (candidate) {
                        target = candidate;
                        break;
                      }
                    }
                    if (target) break;
                  }

                  const selectButton = controls.find(node => clean(node.textContent) === '선택');
                  return {
                    path,
                    routeReady: path.includes('/cnm/selectVisitorCnt'),
                    generalFound: labels.length > 0,
                    targetFound: Boolean(target),
                    targetSelected: selected(target),
                    targetEnabled: Boolean(target && !target.disabled &&
                      target.getAttribute('aria-disabled') !== 'true'),
                    targetClass: target ? String(target.className || '') : '',
                    targetTitle: target ? String(target.title || '') : '',
                    selectFound: Boolean(selectButton),
                    selectEnabled: Boolean(selectButton && !selectButton.disabled &&
                      selectButton.getAttribute('aria-disabled') !== 'true'),
                    modalOpen,
                    seatCount: seatButtons.length,
                  };
                }
                """,
                max(1, int(people)),
            )
            return dict(result) if isinstance(result, dict) else {}
        except Exception:
            return {}

    @staticmethod
    def _click_visitor_count(page, people: int) -> bool:
        try:
            return bool(
                page.evaluate(
                    r"""
                    people => {
                      const clean = value => String(value || '').replace(/\s+/g, '');
                      const visible = node => {
                        if (!node) return false;
                        const style = window.getComputedStyle(node);
                        const rect = node.getBoundingClientRect();
                        return style.display !== 'none' && style.visibility !== 'hidden' &&
                               rect.width > 0 && rect.height > 0;
                      };
                      const labels = Array.from(document.querySelectorAll('body *'))
                        .filter(node => visible(node) && clean(node.textContent) === '일반')
                        .sort((left, right) => {
                          const childGap = left.children.length - right.children.length;
                          if (childGap) return childGap;
                          return left.getBoundingClientRect().width - right.getBoundingClientRect().width;
                        });
                      for (const label of labels) {
                        let box = label;
                        for (let depth = 0; depth < 10 && box; depth += 1, box = box.parentElement) {
                          const target = Array.from(box.querySelectorAll('button')).find(button =>
                            visible(button) && clean(button.textContent) === String(people)
                          );
                          if (!target) continue;
                          if (target.disabled || target.getAttribute('aria-disabled') === 'true') {
                            return false;
                          }
                          target.scrollIntoView({block: 'center', inline: 'center'});
                          target.click();
                          return true;
                        }
                      }
                      return false;
                    }
                    """,
                    max(1, int(people)),
                )
            )
        except Exception:
            return False
