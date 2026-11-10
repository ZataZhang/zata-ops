package com.dewell.keycloak.auth;

import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

import java.util.Map;
import java.util.concurrent.atomic.AtomicReference;
import org.junit.jupiter.api.Test;
import org.keycloak.models.SingleUseObjectProvider;

final class SharedOtpAuthenticatorTest {
    private static final String CURRENT_CHALLENGE_KEY = "otp:current";
    private static final String OLD_CHALLENGE_ID = "challenge-old";
    private static final String NEW_CHALLENGE_ID = "challenge-new";
    private static final String OLD_CHALLENGE_DATA_KEY = "otp:data:" + OLD_CHALLENGE_ID;
    private static final String NEW_CHALLENGE_DATA_KEY = "otp:data:" + NEW_CHALLENGE_ID;

    @Test
    void staleVerificationCannotConsumeAReplacementChallenge() {
        SingleUseObjectProvider store = mock(SingleUseObjectProvider.class);
        AtomicReference<Map<String, String>> currentChallenge = new AtomicReference<>(
            Map.of("challenge_id", OLD_CHALLENGE_ID)
        );
        when(store.get(CURRENT_CHALLENGE_KEY)).thenAnswer(invocation -> currentChallenge.get());
        when(store.remove(OLD_CHALLENGE_DATA_KEY)).thenAnswer(invocation -> {
            currentChallenge.set(Map.of("challenge_id", NEW_CHALLENGE_ID));
            return Map.of("challenge_id", OLD_CHALLENGE_ID);
        });

        boolean consumedCurrentChallenge = SharedOtpAuthenticator.consumeChallengeIfCurrent(
            store,
            CURRENT_CHALLENGE_KEY,
            OLD_CHALLENGE_DATA_KEY,
            OLD_CHALLENGE_ID
        );

        assertFalse(consumedCurrentChallenge);
        verify(store).remove(OLD_CHALLENGE_DATA_KEY);
        verify(store, never()).remove(NEW_CHALLENGE_DATA_KEY);
    }

    @Test
    void challengeCanOnlyBeConsumedOnce() {
        SingleUseObjectProvider store = mock(SingleUseObjectProvider.class);
        when(store.remove(OLD_CHALLENGE_DATA_KEY)).thenReturn(
            Map.of("challenge_id", OLD_CHALLENGE_ID),
            null
        );
        when(store.get(CURRENT_CHALLENGE_KEY)).thenReturn(
            Map.of("challenge_id", OLD_CHALLENGE_ID)
        );

        boolean firstConsumption = SharedOtpAuthenticator.consumeChallengeIfCurrent(
            store,
            CURRENT_CHALLENGE_KEY,
            OLD_CHALLENGE_DATA_KEY,
            OLD_CHALLENGE_ID
        );
        boolean replayedConsumption = SharedOtpAuthenticator.consumeChallengeIfCurrent(
            store,
            CURRENT_CHALLENGE_KEY,
            OLD_CHALLENGE_DATA_KEY,
            OLD_CHALLENGE_ID
        );

        assertTrue(firstConsumption);
        assertFalse(replayedConsumption);
    }
}
