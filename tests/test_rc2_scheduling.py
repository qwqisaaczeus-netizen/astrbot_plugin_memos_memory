import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from astrbot_plugin_memos_memory.external_models import ExternalModelRegistry
from astrbot_plugin_memos_memory.model_tasks import task_key, is_model_setting
from astrbot_plugin_memos_memory.generation_v2.integration import stage_routes_for
from astrbot_plugin_memos_memory.xinchao import XinchaoController


def model(key):
    return {'id':key,'name':key,'model':'fixture','base_url':'https://example.invalid/v1',
            'api_key':'private-fixture-key','enabled':True}


class RoutingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.registry=ExternalModelRegistry(self.root)
        self.registry.save({'models':[model('model_a'),model('model_b')], 'astr_followup_enabled':True,
            'astr_followup_model_id':'model_a','astr_followup_tasks':['memory_generation','creative'],
            'astr_followup_models':{'episode_extract':'model_b','diary_write':'','xinchao_dream':'model_b'}})

    async def asyncTearDown(self):
        await self.registry.close()
        self.temp.cleanup()

    async def test_task_precedence_and_explicit_disable(self):
        for key in ['episode_extract','episode_extract_compact_retry']:
            self.assertEqual(self.registry.followup_provider(key).model_id,'model_b')
        self.assertIsNone(self.registry.followup_provider('diary_render_retry'))
        self.assertEqual(self.registry.followup_provider('narrative_plan').model_id,'model_a')
        self.assertEqual(self.registry.followup_provider('xinchao_dream').model_id,'model_b')
        self.assertEqual(self.registry.followup_provider('xinchao_proactive').model_id,'model_a')

    async def test_reload_old_client_save_and_secret_redaction(self):
        payload=self.registry.payload()
        self.assertNotIn('private-fixture-key',json.dumps(payload))
        payload.pop('astr_followup_models')
        self.registry.save(payload)
        loaded=ExternalModelRegistry(self.root)
        try:
            self.assertEqual(loaded.astr_followup_models,self.registry.astr_followup_models)
            self.assertEqual(loaded.followup_provider('episode_extract').model_id,'model_b')
            self.assertIsNone(loaded.followup_provider('diary_write'))
        finally:
            await loaded.close()

    async def test_map_only_and_master_off(self):
        payload=self.registry.payload()
        payload.update(astr_followup_model_id='',astr_followup_tasks=[])
        self.registry.save(payload)
        self.assertEqual(self.registry.followup_provider('episode_extract').model_id,'model_b')
        self.assertIsNone(self.registry.followup_provider('narrative_plan'))
        payload.update(astr_followup_enabled=False)
        self.registry.save(payload)
        self.assertIsNone(self.registry.followup_provider('episode_extract'))

    async def test_invalid_model_is_rejected_without_mutating_routes(self):
        original=dict(self.registry.astr_followup_models)
        payload=self.registry.payload()
        payload['astr_followup_models']={'diary_review':'missing'}
        with self.assertRaises(ValueError): self.registry.save(payload)
        self.assertEqual(self.registry.astr_followup_models,original)

    async def test_partial_mind_settings_preserve_body_and_secret(self):
        plugin=SimpleNamespace(context=SimpleNamespace(get_all_providers=lambda:[]),
                               _log_event=lambda *args:None)
        controller=XinchaoController(plugin,self.root)
        try:
            controller.save_settings({'body_anchor_date':'2026-06-19','dream_provider_id':'original',
                'perception_api_key':'mind-private','daytime_memory_cooldown_hours':96})
            controller.save_settings({'live_perception_provider_id':'new'})
            self.assertEqual(controller.settings['body_anchor_date'],'2026-06-19')
            self.assertEqual(controller.settings['dream_provider_id'],'original')
            self.assertEqual(controller.settings['daytime_memory_cooldown_hours'],96)
            self.assertEqual(controller._perception_api_key,'mind-private')
            self.assertNotIn('mind-private',json.dumps(controller.settings_payload()))
        finally: await controller.terminate()


class TaskCatalogTests(unittest.TestCase):
    def test_task_aliases_and_model_settings(self):
        self.assertEqual(task_key('profile_llm_manual_replay'),'profile')
        self.assertEqual(task_key('diary_render:retry'),'diary_write')
        self.assertEqual(task_key('episode_extract_compact_retry'),'episode_extract')
        self.assertTrue(is_model_setting('time_insight_llm_provider_id'))
        self.assertTrue(is_model_setting('narrative_plan_provider_id'))
        self.assertFalse(is_model_setting('memos_token'))
        self.assertFalse(is_model_setting('character_name'))

    def test_stage_routes_include_per_task_backup_and_primary(self):
        plugin=SimpleNamespace(diary_render_provider_id='writer',
            _external_models=SimpleNamespace(astr_followup_models={'episode_extract':'backup','diary_review':''}))
        with patch('astrbot_plugin_memos_memory.generation_v2.integration.route_for',
                   side_effect=lambda p,s,provider,task:(provider,task)) as route:
            result=stage_routes_for(plugin,'scope')
            self.assertEqual(result['diary_write'],('writer','diary_write'))
            self.assertEqual(result['episode_extract'],(None,'episode_extract'))
            self.assertEqual(result['diary_review'],(None,'diary_review'))
            self.assertEqual(route.call_count,3)


class ProductionRoutesTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from test_generation_v2_test3 import archive
        from test_generation_v2_test4 import Model
        from astrbot_plugin_memos_memory.generation_v2.adapters import AstrAdapter
        from astrbot_plugin_memos_memory.generation_v2.coordinator import Coordinator
        from astrbot_plugin_memos_memory.generation_v2.literary import DiaryStore
        from astrbot_plugin_memos_memory.generation_v2.scheduler import Route, Scheduler
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root=Path(self.temp.name)
        archive(root/'source.db')
        self.store=DiaryStore(root/'runtime.db')
        self.service=Coordinator(self.store,Scheduler(self.store),root/'source.db')
        self.addAsyncCleanup(self.service.close)
        self.models={key:Model() for key in ['base','episode_extract','narrative_plan','diary_write','diary_review']}
        self.routes={key:Route(key,'astr','fixture','fixture',AstrAdapter(model)) for key,model in self.models.items()}

    async def test_real_pipeline_uses_each_stage_model(self):
        routes={key:(self.routes[key],None) for key in ['episode_extract','narrative_plan','diary_write','diary_review']}
        job=await self.service.start('batch','scope',self.routes['base'],stage_routes=routes)
        self.assertEqual((await self.service.wait(job))['stage'],'draft_ready')
        self.assertEqual(len(self.models['base'].calls),0)
        for key in routes: self.assertEqual(len(self.models[key].calls),1)
        await self.service.start('batch','scope',self.routes['base'],stage_routes=routes)
        await self.service.wait(job)
        for key in routes: self.assertEqual(len(self.models[key].calls),1)

    async def test_pre_rc2_workflow_reuses_its_original_routes(self):
        job=await self.service.start('batch','scope',self.routes['base'])
        await self.service.wait(job)
        await self.service.start('batch','scope',self.routes['base'],
                                 stage_routes={'diary_write':(self.routes['diary_write'],None)})
        await self.service.wait(job)
        self.assertEqual(len(self.models['base'].calls),4)
        self.assertEqual(len(self.models['diary_write'].calls),0)

