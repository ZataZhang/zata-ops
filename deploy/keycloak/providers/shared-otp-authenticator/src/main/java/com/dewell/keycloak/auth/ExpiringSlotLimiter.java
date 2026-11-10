package com.dewell.keycloak.auth;

import java.util.function.Predicate;

/**
 * Uses atomic key reservations to enforce a bounded number of operations.
 *
 * <p>The reservation callback must provide an atomic put-if-absent operation.
 * Each successful caller claims a different slot, so concurrent callers
 * cannot lose increments in a read-modify-write counter.</p>
 */
final class ExpiringSlotLimiter {
    private ExpiringSlotLimiter() {
    }

    static int reserve(String slotKeyPrefix, int maximumSlots, Predicate<String> tryReserveSlot) {
        if (maximumSlots < 1) {
            throw new IllegalArgumentException("maximumSlots must be positive");
        }
        for (int slotIndex = 0; slotIndex < maximumSlots; slotIndex++) {
            if (tryReserveSlot.test(slotKeyPrefix + ":" + slotIndex)) {
                return slotIndex;
            }
        }
        return -1;
    }
}
