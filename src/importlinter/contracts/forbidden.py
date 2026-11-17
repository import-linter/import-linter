from __future__ import annotations

from collections.abc import Collection, Iterable, Iterator
from itertools import islice

from grimp import ImportGraph

from importlinter.application import contract_utils, output, rendering
from importlinter.application.contract_utils import AlertLevel
from importlinter.configuration import settings
from importlinter.domain import fields
from importlinter.domain.contract import Contract, ContractCheck
from importlinter.domain.helpers import module_expressions_to_modules
from importlinter.domain.imports import Module

from ._common import format_line_numbers


class ForbiddenContract(Contract):
    """
    Forbidden contracts check that one set of modules are not imported by another set of modules.
    Indirect imports will also be checked.

    Where the source and forbidden modules overlap (the same module is in both, or, when treated as
    packages, one contains the other), imports within the overlap are not forbidden. For example,
    a source module ``mypackage`` may not import a forbidden module ``mypackage.foo`` from anywhere
    outside ``mypackage.foo``, but ``mypackage.foo`` may import itself. This allows a wildcard such
    as ``mypackage.*`` to be used as a forbidden module even when a source module is one of the
    modules it matches. See the documentation for details.

    Configuration options:
        - source_modules:    A set of Modules that should not import the forbidden modules.
        - forbidden_modules: A set of Modules that should not be imported by the source modules.
        - ignore_imports:    A set of ImportExpressions. These imports will be ignored if the import
                             would cause a contract to be broken, adding it to the set will cause
                             the contract be kept instead. (Optional.)
        - allow_indirect_imports:  Whether to allow indirect imports to forbidden modules.
                             "True" or "true" will be treated as True. (Optional.)
        - unmatched_ignore_imports_alerting: Decides how to report when the expression in the
                             `ignore_imports` set is not found in the graph. Valid values are
                             "none", "warn", "error". Default value is "error".
        - as_packages:       Whether to treat the source and forbidden modules as packages. If
                             False, each of the modules passed in will be treated as a module
                             rather than a package. Default behaviour is True (treat modules as
                             packages).
        - broken_contract_guidance:      Guidance explaining how to fix the contract if it's
                             broken. These are displayed after the contract's violations.
                             (Optional.)
    """

    type_name = "forbidden"

    source_modules = fields.SetField(subfield=fields.ModuleExpressionField())
    forbidden_modules = fields.SetField(subfield=fields.ModuleExpressionField())
    ignore_imports = fields.SetField(subfield=fields.ImportExpressionField(), required=False)
    allow_indirect_imports = fields.BooleanField(required=False, default=False)
    unmatched_ignore_imports_alerting = fields.EnumField(AlertLevel, default=AlertLevel.ERROR)
    as_packages = fields.BooleanField(required=False, default=True)
    broken_contract_guidance = fields.TextField(required=False)

    def check(self, graph: ImportGraph, verbose: bool) -> ContractCheck:
        is_kept = True
        invalid_chains = []

        import_removal = contract_utils.remove_ignored_imports_and_report(
            graph=graph,
            ignore_imports=self.ignore_imports,  # type: ignore
            unmatched_alerting=self.unmatched_ignore_imports_alerting,  # type: ignore
        )

        source_modules = list(
            module_expressions_to_modules(
                graph,
                self.source_modules,  # type: ignore
            )
        )
        forbidden_modules = list(
            module_expressions_to_modules(
                graph,
                self.forbidden_modules,  # type: ignore
            )
        )

        internal_forbidden, external_forbidden = (
            self._partition_modules_by_whether_they_are_internal(forbidden_modules)
        )

        self._check_all_modules_exist_in_graph(source_modules, graph)
        # Internal modules are always in the graph, so a missing one is a mistake.
        self._check_all_modules_exist_in_graph(internal_forbidden, graph)

        self._validate_external_forbidden_modules(external_forbidden)

        # We only need to check for illegal imports for forbidden modules that are in the graph.
        forbidden_modules_in_graph = [m for m in forbidden_modules if m.name in graph.modules]

        def sort_key(module):
            return module.name

        as_packages: bool = self.as_packages  # type: ignore

        # Imports within any overlap between the source and forbidden modules are allowed.
        # The overlaps are found across *all* the source and forbidden modules, rather than for
        # the pair being checked. Otherwise a wildcard contract would report false positives. For
        # example, with source `**` and forbidden `mypackage.catalog`, the pair
        # (`mypackage.catalog.api`, `mypackage.catalog`) overlaps only on `mypackage.catalog.api`,
        # so the pair on its own would report BROKEN `catalog.api -> catalog.internal`.
        # But that import is within `mypackage.catalog`, so the contract should be KEPT.
        # That import overlaps via the pair (`mypackage.catalog`, `mypackage.catalog`).
        #
        # Without as_packages, only the same module overlaps, and those pairs are skipped below.
        package_overlaps = (
            _get_package_overlaps(source_modules, forbidden_modules_in_graph)
            if as_packages
            else set()
        )

        for source_module in sorted(source_modules, key=sort_key):
            for forbidden_module in self._find_forbidden_modules_to_visit(
                source_module, forbidden_modules_in_graph, graph, verbose
            ):
                if source_module == forbidden_module:
                    output.verbose_print(
                        verbose,
                        f"Skipping overlapping modules {source_module} and {forbidden_module}.",
                    )
                    continue
                if as_packages and _are_within_same_overlap(
                    source_module.name, forbidden_module.name, package_overlaps
                ):
                    # Every chain between these modules would be within an overlap, so allowed.
                    # (Skipping now avoids a search whose results would all be discarded.)
                    output.verbose_print(
                        verbose,
                        f"Skipping {source_module} and {forbidden_module}, as imports between "
                        "them are within an overlap of the source and forbidden modules.",
                    )
                    continue

                output.verbose_print(
                    verbose,
                    f"Searching for import chains from {source_module} to {forbidden_module}...",
                )
                with settings.TIMER as timer:
                    subpackage_chain_data = {
                        "upstream_module": forbidden_module.name,
                        "downstream_module": source_module.name,
                        "chains": [],
                    }

                    if str(self.allow_indirect_imports).lower() == "true":
                        chains = self._get_direct_chains(
                            source_module,
                            forbidden_module,
                            graph,
                            as_packages,
                        )
                    else:
                        chains = graph.find_shortest_chains(
                            importer=source_module.name,
                            imported=forbidden_module.name,
                            as_packages=as_packages,
                            # If one module contains the other, search between the inner package
                            # and the rest of the outer package.
                            exclude_shared_descendants=True,
                        )
                    # If the modules overlap (one contains the other), the search above also
                    # includes chains within the overlap. Those are allowed, so discard them.
                    # Chains within an overlap with a different pair are discarded here too.
                    chains = {
                        chain
                        for chain in chains
                        if not _are_within_same_overlap(chain[0], chain[-1], package_overlaps)
                    }
                    if chains:
                        is_kept = False
                        for chain in sorted(chains):
                            chain_data = []
                            for importer, imported in [
                                (chain[i], chain[i + 1]) for i in range(len(chain) - 1)
                            ]:
                                import_details = graph.get_import_details(
                                    importer=importer, imported=imported
                                )
                                line_numbers = tuple(j["line_number"] for j in import_details)
                                chain_data.append(
                                    {
                                        "importer": importer,
                                        "imported": imported,
                                        "line_numbers": line_numbers,
                                    }
                                )
                            subpackage_chain_data["chains"].append(chain_data)  # type: ignore
                if subpackage_chain_data["chains"]:
                    invalid_chains.append(subpackage_chain_data)
                if verbose:
                    chain_count = len(subpackage_chain_data["chains"])
                    pluralized = "s" if chain_count != 1 else ""
                    duration = rendering.format_duration(timer.duration_in_ms)
                    output.print(
                        f"Found {chain_count} illegal chain{pluralized} in {duration}.",
                    )

        # Sorting by upstream and downstream module ensures that the output is deterministic
        # and that the same upstream and downstream modules are always adjacent in the output.
        def chain_sort_key(chain_data):
            return (chain_data["upstream_module"], chain_data["downstream_module"])

        return ContractCheck(
            kept=is_kept,
            warnings=list(import_removal.warnings),
            ignored_import_count=import_removal.ignored_import_count,
            metadata={"invalid_chains": sorted(invalid_chains, key=chain_sort_key)},
        )

    def render_broken_contract(self, check: ContractCheck) -> None:
        count = 0
        for chains_data in check.metadata["invalid_chains"]:
            downstream, upstream = (
                chains_data["downstream_module"],
                chains_data["upstream_module"],
            )
            output.print_error(f"{downstream} is not allowed to import {upstream}:")
            output.new_line()
            count += len(chains_data["chains"])
            for chain in chains_data["chains"]:
                first_line = True
                for direct_import in chain:
                    importer, imported = (
                        direct_import["importer"],
                        direct_import["imported"],
                    )
                    line_numbers = format_line_numbers(direct_import["line_numbers"])
                    import_string = f"{importer} -> {imported} ({line_numbers})"
                    if first_line:
                        output.print_error(f"-   {import_string}", bold=False)
                        first_line = False
                    else:
                        output.indent_cursor()
                        output.print_error(import_string, bold=False)
                output.new_line()

            output.new_line()

        contract_utils.render_broken_contract_guidance(self.broken_contract_guidance)  # type: ignore

    def _check_all_modules_exist_in_graph(
        self, modules: Iterable[Module], graph: ImportGraph
    ) -> None:
        for module in modules:
            if module.name not in graph.modules:
                raise ValueError(f"Module '{module.name}' does not exist.")

    def _validate_external_forbidden_modules(
        self, external_forbidden_modules: Iterable[Module]
    ) -> None:
        if external_forbidden_modules:
            if self._graph_was_built_with_externals():
                for module in external_forbidden_modules:
                    if module.root_package_name != module.name:
                        raise ValueError(
                            f"Invalid forbidden module {module}: "
                            "subpackages of external packages are not valid."
                        )
            else:
                raise ValueError(
                    "The top level configuration must have include_external_packages=True "
                    "when there are external forbidden modules."
                )

    def _partition_modules_by_whether_they_are_internal(
        self, modules: Iterable[Module]
    ) -> tuple[set[Module], set[Module]]:
        internal_modules: set[Module] = set()
        external_modules: set[Module] = set()

        root_packages = [Module(name) for name in self.session_options["root_packages"]]
        for module in modules:
            if any(module.is_in_package(root_package) for root_package in root_packages):
                internal_modules.add(module)
            else:
                external_modules.add(module)

        return internal_modules, external_modules

    def _graph_was_built_with_externals(self) -> bool:
        return str(self.session_options.get("include_external_packages")).lower() == "true"

    def _find_forbidden_modules_to_visit(
        self,
        source_module: Module,
        forbidden_modules: list[Module],
        graph: ImportGraph,
        verbose: bool,
    ) -> list[Module]:
        """
        Return the forbidden modules to search for chains to from the source module, sorted by name.

        We only need to search for chains to the forbidden modules that the source module has
        chains to. A quick check finds those with a single pass over the imports from the source
        module, instead of searching for chains to each forbidden module in turn.

        In verbose mode we report on every pair of modules, so all the forbidden modules are
        returned and the quick check is skipped.
        """
        if verbose:
            return sorted(forbidden_modules, key=lambda module: module.name)

        forbidden_module_names = {module.name for module in forbidden_modules}
        as_packages: bool = self.as_packages  # type: ignore

        if str(self.allow_indirect_imports).lower() == "true":
            imported_names = graph.find_modules_directly_imported_by(
                source_module.name, as_package=as_packages
            )
        else:
            imported_names = graph.find_upstream_modules(
                source_module.name, as_package=as_packages
            )

        if as_packages:
            # Importing a descendant of a forbidden module is importing the forbidden module
            imported_names = {
                ancestor_or_self
                for name in imported_names
                for ancestor_or_self in _get_name_and_ancestors(name)
            }

        forbidden_module_names_to_visit = imported_names & forbidden_module_names

        if as_packages:
            # The quick check above can wrongly rule out a forbidden module that overlaps with the
            # source module (the same module, or one contains the other).
            #
            # When treating the source module as a package, grimp only reports imports of modules
            # *outside* that package. So an import between two modules inside the source package
            # is invisible to the quick check. Such an import can still be forbidden. For example,
            # with source `mypackage` and forbidden `mypackage.catalog`, this is a violation:
            #
            #     mypackage.other -> mypackage.catalog.internal
            #
            # but it is within the source package, so `mypackage.catalog` would never appear in
            # `imported_names`.
            #
            # So always visit the overlapping forbidden modules, and let the real search decide.
            forbidden_module_names_to_visit |= {
                forbidden_module.name
                for forbidden_module in forbidden_modules
                if _get_package_overlap(source_module, forbidden_module)
            }

        return [Module(name) for name in sorted(forbidden_module_names_to_visit)]

    def _get_direct_chains(
        self,
        source_package: Module,
        forbidden_package: Module,
        graph: ImportGraph,
        as_packages: bool,
    ) -> set[tuple[str, ...]]:
        chains: set[tuple[str, ...]] = set()
        source_modules = (
            self._get_all_modules_in_package(source_package, graph)
            if as_packages
            else {source_package}
        )
        forbidden_modules = (
            self._get_all_modules_in_package(forbidden_package, graph)
            if as_packages
            else {forbidden_package}
        )
        for source_module in source_modules:
            imported_module_names = graph.find_modules_directly_imported_by(source_module.name)
            for imported_module_name in imported_module_names:
                imported_module = Module(imported_module_name)
                if imported_module in forbidden_modules:
                    chains.add((source_module.name, imported_module.name))
        return chains

    def _get_all_modules_in_package(self, module: Module, graph: ImportGraph) -> set[Module]:
        """
        Return all the modules in the supplied module, including itself.

        If the module is squashed, it will be treated as a single module.
        """
        importer_modules = {module}
        if not graph.is_module_squashed(module.name):
            importer_modules |= {Module(m) for m in graph.find_descendants(module.name)}
        return importer_modules


def _get_package_overlaps(
    source_modules: Iterable[Module], forbidden_modules: Collection[Module]
) -> set[str]:
    """
    Return the names of the outermost modules that overlap between any source and forbidden
    module, when treated as packages.

    Overlaps nested inside others are dropped, so the packages of the result are disjoint: each
    module is in the package of at most one overlap.
    """
    overlaps = {
        overlap.name
        for source_module in source_modules
        for forbidden_module in forbidden_modules
        if (overlap := _get_package_overlap(source_module, forbidden_module))
    }

    # Drop any overlap that is inside another overlap, as the outer one already covers it. For
    # example, given `mypackage.one` and `mypackage.one.two`, keep only `mypackage.one`. The first
    # name yielded by _get_name_and_ancestors is the overlap itself, so skip it.
    reduced_overlaps = {
        overlap
        for overlap in overlaps
        if not any(
            ancestor in overlaps for ancestor in islice(_get_name_and_ancestors(overlap), 1, None)
        )
    }

    return reduced_overlaps


def _get_package_overlap(source_module: Module, forbidden_module: Module) -> Module | None:
    """
    Return the module that the source and forbidden modules have in common, when treated as
    packages, if any.

    The packages overlap if they are the same module, or if one contains the other. The overlap is
    then the inner of the two modules, as every module in its package is in both packages.
    """
    # Compare names, rather than using Module.is_in_package, as this is called for every pair of
    # source and forbidden modules.
    source_name, forbidden_name = source_module.name, forbidden_module.name
    if source_name == forbidden_name:
        return source_module
    if source_name.startswith(f"{forbidden_name}."):
        return source_module
    if forbidden_name.startswith(f"{source_name}."):
        return forbidden_module
    return None


def _are_within_same_overlap(importer: str, imported: str, package_overlaps: set[str]) -> bool:
    """
    Return whether both modules are in the package of the same overlap.

    Imports between such modules are allowed. The overlaps must be disjoint, as returned by
    _get_package_overlaps.
    """
    if not package_overlaps:
        # Quick exit, as this is called for every pair of modules, and every chain.
        return False
    overlap = _get_enclosing_overlap(importer, package_overlaps)
    return overlap is not None and overlap == _get_enclosing_overlap(imported, package_overlaps)


def _get_enclosing_overlap(name: str, overlaps: set[str]) -> str | None:
    """
    Return the overlap whose package contains the module (or is the module), if any.

    The overlaps must be disjoint, so there is at most one. For example, given the overlaps
    {"mypackage.one"}:

        "mypackage.one"         -> "mypackage.one"
        "mypackage.one.blue"    -> "mypackage.one"
        "mypackage.two"         -> None
    """
    for candidate in _get_name_and_ancestors(name):
        if candidate in overlaps:
            return candidate
    return None


def _get_name_and_ancestors(name: str) -> Iterator[str]:
    """
    Yield the module name, followed by the names of each of its ancestors.

    For example, "mypackage.one.blue" yields "mypackage.one.blue", "mypackage.one", "mypackage".
    """
    while True:
        yield name
        if "." not in name:
            return
        name = name.rsplit(".", 1)[0]
