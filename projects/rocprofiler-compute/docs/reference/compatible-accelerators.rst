.. meta::
   :description: ROCm Compute Profiler support: compatible accelerators and GPUs
   :keywords: Omniperf, compatible, cdna, gcn, gfx, rdna, radeon, ryzen, hardware, architecture

***********************
Compatible GPUs/APUs
***********************

The following table lists SoC (System on Chip) support in
ROCm Compute Profiler. See :doc:`rocm:reference/gpu-arch-specs` for full AMD Instinct™ GPUs and AMD Ryzen™ APUs specifications.

.. _def-soc:

.. note::

   In ROCm Compute Profiler documentation, the term System on Chip (SoC) refers to a
   particular family of AMD GPUs/APUs.

.. tab-set::

  .. tab-item:: AMD Instinct GPUs

    .. list-table::
      :header-rows: 1

      * - Platform
        - Status

      * - AMD Instinct MI350 Series
        - Supported ✅

      * - AMD Instinct MI300 Series
        - Supported ✅

      * - AMD Instinct MI200 Series
        - Supported ✅

      * - AMD Instinct MI100 Series
        - Supported ✅

      * - AMD Instinct MI50, MI60 (Vega 20)
        - No support ❌



  .. tab-item:: AMD Ryzen APUs

    .. list-table::
      :header-rows: 1

      * - Platform
        - Status

      * - AMD Ryzen AI Max / Ryzen™ AI Max+ 300 and 400 Series integrated graphics (Strix/Halo, Gorgon/Halo, Gorgon/Point gfx1153)
        - Supported ✅ (see :doc:`/conceptual/rdna/rdna-performance-model`)

  .. tab-item:: AMD Radeon GPUs

    .. list-table::
      :header-rows: 1

      * - Platform
        - Status

      * - AMD Radeon RX 9070 Series / Radeon AI PRO R9700 (RDNA4, gfx1201)
        - Counter profiling and analysis. Roofline and GCEA analysis are not available.

    gfx1201 uses the RDNA panel layout with architecture-specific counter
    definitions. VALU throughput is reported as instructions, not FLOPs.
    GL2-to-fabric traffic measures requests at that interface; it does not
    measure physical GDDR traffic.
