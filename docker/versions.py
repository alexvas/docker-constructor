#!/usr/bin/env python3
"""Backward-compatible re-export shim that delegates to docker.versioning.

All public types and functions are available through ``docker.versioning``
directly.  This module exists only so that existing ``from docker.versions
import ...`` statements in internal tests continue to resolve.
"""

from __future__ import annotations

if __package__:
    from .versioning.inventory import load_inventory, validate_inventory
    from .versioning.errors import (
        VersionConfigError,
        InventoryError,
        VersionSyntaxError,
        ConstraintSyntaxError,
    )
    from .versioning.constraints import (
        NumericVersion,
        ConstraintClause,
        Constraint,
        parse_numeric_version,
        parse_constraint,
        validate_constraint_consistency,
    )
    from .versioning.model import (
        ArtifactEntry,
        BaseStage,
        BuildInventory,
        DockerRegistrySource,
        DockerRegistryUpdate,
        FdPrebuiltStage,
        GitHubReleaseSource,
        GitHubReleaseUpdate,
        GitRefUpdate,
        GitSource,
        Inventory,
        NodeEntry,
        NpmArtifact,
        NpmSource,
        NpmToolEntry,
        NpmUpdate,
        PiReleaseSource,
        PiToolEntry,
        OhMyZshEntry,
        OpenSpecToolsStage,
        OverridePolicy,
        PiExtensionEntry,
        PiToolsStage,
        PrebuiltToolEntry,
        PyPiSource,
        PyPiUpdate,
        PythonEntry,
        RtkPrebuiltStage,
        RuntimeInventory,
        RuntimeStage,
        RuntimeValidation,
        RustChannelSource,
        RustChannelUpdate,
        RustEntry,
        Stages,
        ToolchainStage,
        TyEntry,
        UvEntry,
        UvPythonSource,
        UvPythonUpdate,
    )
else:
    from versioning.inventory import load_inventory, validate_inventory  # type: ignore[import-not-found]
    from versioning.errors import (  # type: ignore[import-not-found]
        VersionConfigError,
        InventoryError,
        VersionSyntaxError,
        ConstraintSyntaxError,
    )
    from versioning.constraints import (  # type: ignore[import-not-found]
        NumericVersion,
        ConstraintClause,
        Constraint,
        parse_numeric_version,
        parse_constraint,
        validate_constraint_consistency,
    )
    from versioning.model import (  # type: ignore[import-not-found]
        ArtifactEntry,
        BaseStage,
        BuildInventory,
        DockerRegistrySource,
        DockerRegistryUpdate,
        FdPrebuiltStage,
        GitHubReleaseSource,
        GitHubReleaseUpdate,
        GitRefUpdate,
        GitSource,
        Inventory,
        NodeEntry,
        NpmArtifact,
        NpmSource,
        NpmToolEntry,
        NpmUpdate,
        PiReleaseSource,
        PiToolEntry,
        OhMyZshEntry,
        OpenSpecToolsStage,
        OverridePolicy,
        PiExtensionEntry,
        PiToolsStage,
        PrebuiltToolEntry,
        PyPiSource,
        PyPiUpdate,
        PythonEntry,
        RtkPrebuiltStage,
        RuntimeInventory,
        RuntimeStage,
        RuntimeValidation,
        RustChannelSource,
        RustChannelUpdate,
        RustEntry,
        Stages,
        ToolchainStage,
        TyEntry,
        UvEntry,
        UvPythonSource,
        UvPythonUpdate,
    )
