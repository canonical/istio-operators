import asyncio
from random import choices
from string import ascii_lowercase, digits

import pytest
import juju.model


def pytest_addoption(parser):
    parser.addoption(
        "--client-model",
        action="store",
        help="Name of client model to use; if not provided, will "
        "create one and clean it up after.",
    )


@pytest.fixture
async def client_model(ops_test, request):
    # TODO: fold this into pytest-operator
    model_name = request.config.option.client_model
    if not client_model:
        cleanup = True
        module_name = request.module.__name__.rpartition(".")[-1]
        suffix = "".join(choices(ascii_lowercase + digits, k=4))
        model_name = f"{module_name.replace('_', '-')}-client-{suffix}"
        model = await ops_test._controller.add_model(model_name, cloud_name=ops_test.cloud_name)
        # NB: This call to `juju models` is needed because libjuju's
        # `add_model` doesn't update the models.yaml cache that the Juju
        # CLI depends on with the model's UUID, which the CLI requires to
        # connect. Calling `juju models` beforehand forces the CLI to
        # update the cache from the controller.
        await ops_test.juju("models")
    else:
        cleanup = False
        model = juju.model.Model()
        await model.connect(model_name)
    try:
        yield model
    finally:
        if cleanup:
            await asyncio.gather(*(app.remove() for app in model.applications))
            await model.wait_for_idle()
            await model.disconnect()
            await ops_test._controller.destroy_model(model_name)
