package com.dewell.keycloak.auth;

import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

import java.util.stream.Stream;
import org.junit.jupiter.api.Test;
import org.keycloak.models.KeycloakSession;
import org.keycloak.models.RealmModel;
import org.keycloak.models.UserModel;
import org.keycloak.models.UserProvider;

final class SharedBindContactActionTest {
    private static final String EMAIL = "user@example.com";
    private static final String PHONE = "+8613800000000";

    @Test
    void contactHeldByAnotherAccountIsDetectedEvenWhenUnverified() {
        KeycloakSession session = mock(KeycloakSession.class);
        UserProvider users = mock(UserProvider.class);
        RealmModel realm = mock(RealmModel.class);
        UserModel currentUser = userWithId("current");
        UserModel otherUser = userWithId("other");
        when(session.users()).thenReturn(users);
        when(otherUser.isEmailVerified()).thenReturn(false);

        when(users.getUserByEmail(realm, EMAIL)).thenReturn(otherUser);
        assertTrue(OtpChallenges.heldByAnotherUser(session, realm, "email", EMAIL, currentUser));

        when(users.getUserByEmail(realm, EMAIL)).thenReturn(currentUser);
        assertFalse(OtpChallenges.heldByAnotherUser(session, realm, "email", EMAIL, currentUser));

        when(users.searchForUserByUserAttributeStream(realm, "phone_number", PHONE))
            .thenAnswer(invocation -> Stream.of(currentUser, otherUser));
        assertTrue(OtpChallenges.heldByAnotherUser(session, realm, "sms", PHONE, currentUser));
    }

    @Test
    void boundContactIsMarkedVerified() {
        UserModel user = mock(UserModel.class);

        SharedBindContactAction.applyContact(user, "email", EMAIL);
        SharedBindContactAction.applyContact(user, "sms", PHONE);

        verify(user).setEmail(EMAIL);
        verify(user).setEmailVerified(true);
        verify(user).setSingleAttribute("phone_number", PHONE);
        verify(user).setSingleAttribute("phone_number_verified", "true");
    }

    private static UserModel userWithId(String id) {
        UserModel user = mock(UserModel.class);
        when(user.getId()).thenReturn(id);
        return user;
    }
}
