Removed the in-UI inspiration-publish and GitHub-login popups from the system interface (frontend modals, their WebSocket events, and the backend inspiration/github-auth services, endpoints, and models).

Publishing an inspiration is now confirmed directly in chat by the /publish-inspiration skill, and GitHub authentication uses latchkey/terminal login instead of a browser popup.

The /publish-inspiration skill now requires the publishing agent to replace the generated placeholder thumbnail with an SVG it draws to look like the actual app (real layout, colors, and title), and to iterate on it from the user's plain-language feedback in chat -- users never see or edit SVG markup.
