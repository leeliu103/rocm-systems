// Copyright © Advanced Micro Devices, Inc., or its affiliates.
// SPDX-License-Identifier: MIT

#include <gtest/gtest.h>
#include "lib/aqlprofile/core/pm4_factory.h"

#include <string>
#include <utility>
#include <vector>

using namespace aql_profile;

namespace
{
// Helper to create a valid agent info struct
aqlprofile_agent_info_v1_t
makeTestAgentInfo(const char* gfxip = "gfx900")
{
    aqlprofile_agent_info_v1_t info{};
    info.agent_gfxip          = strdup(gfxip);
    info.cu_num               = 64;
    info.se_num               = 4;
    info.xcc_num              = 1;
    info.shader_arrays_per_se = 2;
    info.domain               = 0;
    info.location_id          = 0x1234;
    return info;
}

}  // namespace

// Test: Register agent and retrieve info (happy path)
TEST(Pm4FactoryTest, RegisterAgentAndGetAgentInfo)
{
    auto                      agentInfo = makeTestAgentInfo();
    aqlprofile_agent_handle_t handle    = RegisterAgent(&agentInfo);
    const AgentInfo*          info      = GetAgentInfo(handle);
    ASSERT_NE(info, nullptr) << "AgentInfo should not be null";
    EXPECT_EQ(info->cu_num, 64u);
    EXPECT_EQ(info->se_num, 4u);
    EXPECT_EQ(info->xcc_num, 1u);
    EXPECT_EQ(info->shader_arrays_per_se, 2u);
}

// Test: GetAgentInfo returns nullptr for invalid handle
TEST(Pm4FactoryTest, GetAgentInfoInvalidHandleReturnsNull)
{
    aqlprofile_agent_handle_t invalidHandle{};
    invalidHandle.handle  = 99999;  // unlikely to exist
    const AgentInfo* info = GetAgentInfo(invalidHandle);
    EXPECT_EQ(info, nullptr);
}

// Test: gfx117X resolves to the dedicated factory id rather than falling back
// to the generic GFX11 id.
TEST(Pm4FactoryTest, GetGpuIdGfx117x)
{
    EXPECT_EQ(Pm4Factory::GetGpuId("gfx1170"), GFX117X_GPU_ID);
    EXPECT_EQ(Pm4Factory::GetGpuId("gfx1171"), GFX117X_GPU_ID);
}

// Test: the gfxip table is matched by ordered prefix, so adding "gfx117" must
// not shadow the neighbouring entries matched by shorter or longer prefixes.
TEST(Pm4FactoryTest, GetGpuIdPrefixOrderingIsPreserved)
{
    EXPECT_EQ(Pm4Factory::GetGpuId("gfx1100"), GFX11_GPU_ID);
    EXPECT_EQ(Pm4Factory::GetGpuId("gfx1151"), GFX115X_GPU_ID);
    EXPECT_EQ(Pm4Factory::GetGpuId("gfx1200"), GFX12_GPU_ID);
    EXPECT_EQ(Pm4Factory::GetGpuId("gfx1250"), MI450_GPU_ID);
    EXPECT_EQ(Pm4Factory::GetGpuId("gfx942"), MI300_GPU_ID);
    EXPECT_EQ(Pm4Factory::GetGpuId("gfx950"), MI350_GPU_ID);
    EXPECT_EQ(Pm4Factory::GetGpuId("gfxJunk"), INVAL_GPU_ID);
}

// Test: the enumerator sits between GFX11 and GFX12, which the relational
// comparisons used for PM4 dispatch and data collection rely on.
TEST(Pm4FactoryTest, Gfx117xGpuIdOrdering)
{
    EXPECT_GT(GFX117X_GPU_ID, GFX11_GPU_ID);
    EXPECT_LT(GFX117X_GPU_ID, GFX12_GPU_ID);
    EXPECT_GT(GFX117X_GPU_ID, MI350_GPU_ID);
}

TEST(Pm4FactoryTest, HarvestedAgentsKeepOwnCounterLayout)
{
    for(const char* name : {"gfx1200", "gfx1201"})
    {
        for(bool reverse : {false, true})
        {
            // Equal CU counts, but active WGPs {0,1,2} versus {0,1,3} in each SA.
            const std::string gfxip =
                std::string(name) + (reverse ? "-harvest-first" : "-contig-first");
            aqlprofile_agent_info_v2_t info{};
            info.agent_gfxip            = gfxip.c_str();
            info.se_num                 = strcmp(name, "gfx1200") == 0 ? 2 : 4;
            info.cu_num                 = info.se_num * 12;
            info.xcc_num                = 1;
            info.shader_arrays_per_se   = 2;
            const uint32_t sa_count     = info.se_num * info.shader_arrays_per_se;
            auto           make_factory = [&](uint32_t mask) {
                for(uint32_t se = 0; se < info.se_num; ++se)
                    for(uint32_t sa = 0; sa < info.shader_arrays_per_se; ++sa)
                        info.cu_bitmap.bits[se][sa] = mask;
                return Pm4Factory::Create(RegisterAgent(&info));
            };

            auto* first  = make_factory(reverse ? 0xcf : 0x3f);
            auto* second = make_factory(reverse ? 0x3f : 0xcf);
            EXPECT_NE(first, second);
            EXPECT_EQ(first, make_factory(reverse ? 0xcf : 0x3f));

            for(auto [factory, harvested] :
                {std::pair{first, reverse}, std::pair{second, !reverse}})
            {
                const uint32_t span = harvested ? 4 : 3;
                ASSERT_EQ(factory->GetNumWGPs(), span);
                ASSERT_EQ(factory->GetNumEvents(HSA_VEN_AMD_AQLPROFILE_BLOCK_NAME_SQ),
                          sa_count * span);

                counter_des_t counter{};
                counter.block_des.id = HSA_VEN_AMD_AQLPROFILE_BLOCK_NAME_SQ;
                counter.block_info   = factory->GetBlockInfo(counter.block_des.id);
                pm4_builder::counters_vector counters;
                counters.push_back(counter);
                std::vector<uint32_t>  data(sa_count * span * 2, 0xdeadbeef);
                pm4_builder::CmdBuffer commands;
                EXPECT_EQ(factory->GetPmcBuilder()->Read(&commands, counters, data.data()),
                          data.size() * sizeof(uint32_t));
                for(uint32_t sa = 0; sa < sa_count; ++sa)
                    for(uint32_t wgp = 0; wgp < span; ++wgp)
                        EXPECT_EQ(data[(sa * span + wgp) * 2],
                                  harvested && wgp == 2 ? 0u : 0xdeadbeefu);
            }
        }
    }
}
