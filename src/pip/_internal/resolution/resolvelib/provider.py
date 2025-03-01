import math
from functools import lru_cache
from typing import (
    TYPE_CHECKING,
    Dict,
    Iterable,
    Iterator,
    Mapping,
    Sequence,
    TypeVar,
    Union,
)

from pip._vendor.resolvelib.providers import AbstractProvider

from .base import Candidate, Constraint, Requirement
from .candidates import REQUIRES_PYTHON_IDENTIFIER
from .factory import Factory

if TYPE_CHECKING:
    from pip._vendor.resolvelib.providers import Preference
    from pip._vendor.resolvelib.resolvers import RequirementInformation

    PreferenceInformation = RequirementInformation[Requirement, Candidate]

    _ProviderBase = AbstractProvider[Requirement, Candidate, str]
else:
    _ProviderBase = AbstractProvider

# Notes on the relationship between the provider, the factory, and the
# candidate and requirement classes.
#
# The provider is a direct implementation of the resolvelib class. Its role
# is to deliver the API that resolvelib expects.
#
# Rather than work with completely abstract "requirement" and "candidate"
# concepts as resolvelib does, pip has concrete classes implementing these two
# ideas. The API of Requirement and Candidate objects are defined in the base
# classes, but essentially map fairly directly to the equivalent provider
# methods. In particular, `find_matches` and `is_satisfied_by` are
# requirement methods, and `get_dependencies` is a candidate method.
#
# The factory is the interface to pip's internal mechanisms. It is stateless,
# and is created by the resolver and held as a property of the provider. It is
# responsible for creating Requirement and Candidate objects, and provides
# services to those objects (access to pip's finder and preparer).


D = TypeVar("D")
V = TypeVar("V")


def _get_with_identifier(
    mapping: Mapping[str, V],
    identifier: str,
    default: D,
) -> Union[D, V]:
    """Get item from a package name lookup mapping with a resolver identifier.

    This extra logic is needed when the target mapping is keyed by package
    name, which cannot be directly looked up with an identifier (which may
    contain requested extras). Additional logic is added to also look up a value
    by "cleaning up" the extras from the identifier.
    """
    if identifier in mapping:
        return mapping[identifier]
    # HACK: Theoretically we should check whether this identifier is a valid
    # "NAME[EXTRAS]" format, and parse out the name part with packaging or
    # some regular expression. But since pip's resolver only spits out three
    # kinds of identifiers: normalized PEP 503 names, normalized names plus
    # extras, and Requires-Python, we can cheat a bit here.
    name, open_bracket, _ = identifier.partition("[")
    if open_bracket and name in mapping:
        return mapping[name]
    return default


class PipProvider(_ProviderBase):
    """Pip's provider implementation for resolvelib.

    :params constraints: A mapping of constraints specified by the user. Keys
        are canonicalized project names.
    :params ignore_dependencies: Whether the user specified ``--no-deps``.
    :params upgrade_strategy: The user-specified upgrade strategy.
    :params user_requested: A set of canonicalized package names that the user
        supplied for pip to install/upgrade.
    """

    def __init__(
        self,
        factory: Factory,
        constraints: Dict[str, Constraint],
        ignore_dependencies: bool,
        upgrade_strategy: str,
        user_requested: Dict[str, int],
    ) -> None:
        self._factory = factory
        self._constraints = constraints
        self._ignore_dependencies = ignore_dependencies
        self._upgrade_strategy = upgrade_strategy
        self._user_requested = user_requested

    def identify(self, requirement_or_candidate: Union[Requirement, Candidate]) -> str:
        return requirement_or_candidate.name

    def narrow_requirement_selection(
        self,
        identifiers: Iterable[str],
        resolutions: Mapping[str, Candidate],
        candidates: Mapping[str, Iterator[Candidate]],
        information: Mapping[str, Iterator["PreferenceInformation"]],
        backtrack_causes: Sequence["PreferenceInformation"],
    ) -> Iterable[str]:
        """Produce a subset of identifiers that should be considered before others.

        Currently pip narrows the following selection:
            * Requires-Python is always considered first.
        """
        for identifier in identifiers:
            # Requires-Python has only one candidate and the check is basically
            # free, so we always do it first to avoid needless work if it fails.
            # This skips calling get_preference() for all other identifiers.
            if identifier == REQUIRES_PYTHON_IDENTIFIER:
                return [identifier]

        return identifiers

    def get_preference(
        self,
        identifier: str,
        resolutions: Mapping[str, Candidate],
        candidates: Mapping[str, Iterator[Candidate]],
        information: Mapping[str, Iterable["PreferenceInformation"]],
        backtrack_causes: Sequence["PreferenceInformation"],
    ) -> "Preference":
        """Produce a sort key for given requirement based on preference.

        The lower the return value is, the more preferred this group of
        arguments is.

        Currently pip considers the following in order:

        * Prefer if any of the known requirements is "direct", e.g. points to an
          explicit URL.
        * If equal, prefer if any requirement is "pinned", i.e. contains
          operator ``===`` or ``==``.
        * Order user-specified requirements by the order they are specified.
        * If equal, prefers "non-free" requirements, i.e. contains at least one
          operator, such as ``>=`` or ``<``.
        * If equal, order alphabetically for consistency (helps debuggability).
        """
        try:
            next(iter(information[identifier]))
        except StopIteration:
            # There is no information for this identifier, so there's no known
            # candidates.
            has_information = False
        else:
            has_information = True

        if has_information:
            lookups = (r.get_candidate_lookup() for r, _ in information[identifier])
            candidate, ireqs = zip(*lookups)
        else:
            candidate, ireqs = None, ()

        operators = [
            specifier.operator
            for specifier_set in (ireq.specifier for ireq in ireqs if ireq)
            for specifier in specifier_set
        ]

        direct = candidate is not None
        pinned = any(op[:2] == "==" for op in operators)
        unfree = bool(operators)
        requested_order = self._user_requested.get(identifier, math.inf)

        # Prefer the causes of backtracking on the assumption that the problem
        # resolving the dependency tree is related to the failures that caused
        # the backtracking
        backtrack_cause = self.is_backtrack_cause(identifier, backtrack_causes)

        return (
            not direct,
            not pinned,
            not backtrack_cause,
            requested_order,
            not unfree,
            identifier,
        )

    def find_matches(
        self,
        identifier: str,
        requirements: Mapping[str, Iterator[Requirement]],
        incompatibilities: Mapping[str, Iterator[Candidate]],
    ) -> Iterable[Candidate]:
        def _eligible_for_upgrade(identifier: str) -> bool:
            """Are upgrades allowed for this project?

            This checks the upgrade strategy, and whether the project was one
            that the user specified in the command line, in order to decide
            whether we should upgrade if there's a newer version available.

            (Note that we don't need access to the `--upgrade` flag, because
            an upgrade strategy of "to-satisfy-only" means that `--upgrade`
            was not specified).
            """
            if self._upgrade_strategy == "eager":
                return True
            elif self._upgrade_strategy == "only-if-needed":
                user_order = _get_with_identifier(
                    self._user_requested,
                    identifier,
                    default=None,
                )
                return user_order is not None
            return False

        constraint = _get_with_identifier(
            self._constraints,
            identifier,
            default=Constraint.empty(),
        )
        return self._factory.find_candidates(
            identifier=identifier,
            requirements=requirements,
            constraint=constraint,
            prefers_installed=(not _eligible_for_upgrade(identifier)),
            incompatibilities=incompatibilities,
            is_satisfied_by=self.is_satisfied_by,
        )

    @lru_cache(maxsize=None)
    def is_satisfied_by(self, requirement: Requirement, candidate: Candidate) -> bool:
        return requirement.is_satisfied_by(candidate)

    def get_dependencies(self, candidate: Candidate) -> Sequence[Requirement]:
        with_requires = not self._ignore_dependencies
        return [r for r in candidate.iter_dependencies(with_requires) if r is not None]

    @staticmethod
    def is_backtrack_cause(
        identifier: str, backtrack_causes: Sequence["PreferenceInformation"]
    ) -> bool:
        for backtrack_cause in backtrack_causes:
            if identifier == backtrack_cause.requirement.name:
                return True
            if backtrack_cause.parent and identifier == backtrack_cause.parent.name:
                return True
        return False
