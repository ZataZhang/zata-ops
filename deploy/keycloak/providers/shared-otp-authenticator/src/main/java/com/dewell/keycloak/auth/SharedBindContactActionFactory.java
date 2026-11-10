package com.dewell.keycloak.auth;

import org.keycloak.Config;
import org.keycloak.authentication.RequiredActionFactory;
import org.keycloak.authentication.RequiredActionProvider;
import org.keycloak.models.KeycloakSession;
import org.keycloak.models.KeycloakSessionFactory;

/** Registers the bind-email and bind-phone required actions; applications start them with kc_action. */
public abstract class SharedBindContactActionFactory implements RequiredActionFactory {
    private final String providerId;
    private final String displayText;
    private final SharedBindContactAction action;

    SharedBindContactActionFactory(String providerId, String displayText, String channel) {
        this.providerId = providerId;
        this.displayText = displayText;
        this.action = new SharedBindContactAction(channel);
    }

    @Override
    public String getId() {
        return providerId;
    }

    @Override
    public String getDisplayText() {
        return displayText;
    }

    @Override
    public RequiredActionProvider create(KeycloakSession session) {
        return action;
    }

    @Override
    public void init(Config.Scope config) {
    }

    @Override
    public void postInit(KeycloakSessionFactory factory) {
    }

    @Override
    public void close() {
    }

    public static final class Email extends SharedBindContactActionFactory {
        public static final String PROVIDER_ID = "shared-bind-email";

        public Email() {
            super(PROVIDER_ID, "Bind verified email", "email");
        }
    }

    public static final class Phone extends SharedBindContactActionFactory {
        public static final String PROVIDER_ID = "shared-bind-phone";

        public Phone() {
            super(PROVIDER_ID, "Bind verified phone number", "sms");
        }
    }
}
