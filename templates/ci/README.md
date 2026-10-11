# CI templates

The CI every product runs: one workflow that checks, builds, smoke-tests and deploys each push to staging, and one the
owner starts to deploy prod. A product copies both unchanged; the deploy-shape check refuses a copy that differs.
