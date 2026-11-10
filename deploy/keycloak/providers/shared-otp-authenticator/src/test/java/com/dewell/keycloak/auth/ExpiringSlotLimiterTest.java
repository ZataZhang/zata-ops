package com.dewell.keycloak.auth;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.Set;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.ConcurrentHashMap;
import org.junit.jupiter.api.Test;

final class ExpiringSlotLimiterTest {
    private static final int MAXIMUM_SLOTS = 5;
    private static final int CONCURRENT_CALLERS = 16;

    @Test
    void concurrentRequestsCannotReserveMoreThanTheConfiguredLimit() throws Exception {
        Set<String> reservedKeys = ConcurrentHashMap.newKeySet();
        ExecutorService executor = Executors.newFixedThreadPool(CONCURRENT_CALLERS);
        CountDownLatch readyCallers = new CountDownLatch(CONCURRENT_CALLERS);
        CountDownLatch startCallers = new CountDownLatch(1);
        List<Future<Integer>> reservationResults = new ArrayList<>();

        try {
            for (int callerIndex = 0; callerIndex < CONCURRENT_CALLERS; callerIndex++) {
                reservationResults.add(executor.submit(() -> {
                    readyCallers.countDown();
                    assertTrue(startCallers.await(Duration.ofSeconds(5).toMillis(), TimeUnit.MILLISECONDS));
                    return ExpiringSlotLimiter.reserve(
                        "otp-attempt",
                        MAXIMUM_SLOTS,
                        reservedKeys::add
                    );
                }));
            }

            assertTrue(readyCallers.await(Duration.ofSeconds(5).toMillis(), TimeUnit.MILLISECONDS));
            startCallers.countDown();

            int successfulReservations = 0;
            for (Future<Integer> reservationResult : reservationResults) {
                if (reservationResult.get() >= 0) {
                    successfulReservations++;
                }
            }

            assertEquals(MAXIMUM_SLOTS, successfulReservations);
            assertEquals(MAXIMUM_SLOTS, reservedKeys.size());
            assertFalse(reservedKeys.add("otp-attempt:0"));
        } finally {
            startCallers.countDown();
            executor.shutdownNow();
            assertTrue(executor.awaitTermination(5, TimeUnit.SECONDS));
        }
    }
}
