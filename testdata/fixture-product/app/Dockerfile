# The notes app's image: the page, served on port 8000.
FROM busybox:1.37
ARG NOTES_BUILD_COMMIT
ENV NOTES_BUILD_COMMIT=${NOTES_BUILD_COMMIT}
COPY web/ /srv/notes/web/
EXPOSE 8000
CMD ["httpd", "-f", "-p", "8000", "-h", "/srv/notes/web"]
