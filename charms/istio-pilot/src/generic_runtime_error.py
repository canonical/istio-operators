# Copied from https://github.com/canonical/charmed-kubeflow-chisme/blob/main/src/charmed_kubeflow_chisme/exceptions/_generic_charm_runtime_error.py
# TODO: Use directly from Chisme when the pyyaml version conflict between ops and sdi is solved by
#  [this pr](https://github.com/canonical/serialized-data-interface/pull/45)
class GenericCharmRuntimeError(Exception):
    """Raised when the unit should be in ErrorStatus with a message to show in juju status.
    This exception can be used in the charm code to indicate that there is an issue in runtime
    caused by any type of error.
    A typical usage might be:
    ```python
    from charmed_kubeflow_chisme.exceptions import GenericCharmRuntimeError
    try:
        some_function()
    except SomeErrorOfTheFunction as e:
        raise GenericCharmRuntimeError("Some function failed because x and y") from e
    """

    __module__ = None

    def __init__(self, msg: str):
        super().__init__(str(msg))
        self.msg = str(msg)
